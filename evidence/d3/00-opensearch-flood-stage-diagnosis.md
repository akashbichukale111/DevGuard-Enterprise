# The search and graph indices were silently empty

**Date:** 2026-08-01 · Found while verifying the ML model registration.

This is recorded because it invalidates none of the ingestion evidence, but it
does explain why "the UI lineage graph did not render the upstream nodes" was
**not** an index lag. It was data loss at the index layer, and it was still happening.

## Symptom

After emitting the `mlModel` successfully (GMS returned 200, the aspect read back
from the aspect store), the model was invisible to search and had no graph edges:

```
GET /openapi/v3/relationship/mlmodel/<urn>?direction=OUTGOING   ->  {"results":[]}
GraphQL dataset(...user_order_features).lineage(UPSTREAM)       ->  total 0
GraphQL dataset(...user_order_features).lineage(DOWNSTREAM)     ->  total 0
```

The last one is the tell: ingestion **proved** `stg_users` / `stg_orders` are upstreams
of `user_order_features`. If a known-good edge also reads back as zero, the
problem is not the mlModel.

## Root cause

Index doc counts, before the fix:

```
datasetindex_v2      1     (ingestion wrote 10 datasets)
mlmodelindex_v2      0     (just emitted 1)
graph_service_v1    20     (all 20 from the write-path proof; none from ingestion)
```

The aspect store (MySQL) had everything — 705 rows. The OpenSearch side had the earlier
writes and nothing after. GMS logs give the reason, and it was still live:

```
ERROR c.l.m.s.e.update.BulkListener:53 - Failed to feed bulk request 3.
[0]: index [mlmodelindex_v2], id [urn%3Ali%3AmlModel%3A...devguard_churn_risk...],
     message [OpenSearchException[type=cluster_block_exception,
     reason=index [mlmodelindex_v2] blocked by:
     [TOO_MANY_REQUESTS/12/disk usage exceeded flood-stage watermark,
      index has read-only-allow-delete block]]]
```

**82 of 82 indices carried `index.blocks.read_only_allow_delete`**, and
`cluster.blocks.create_index: true` was set persistently — OpenSearch's
`DiskThresholdMonitor` applies both when a node crosses the flood-stage
watermark. The MAE consumer read every message (Kafka lag was **0** on all
partitions), attempted the bulk write, logged the rejection and committed the
offset anyway. **Nothing retries those writes.** The metadata was durable in
MySQL and permanently absent from the index.

The watermark maths is why this kept recurring: the filesystem is 252 GB with a
much smaller writable allowance, so OpenSearch computed `98%` used from
`4.3gb avail / 251.9gb total` and tripped the default 95% flood stage. The
earlier session raised the watermarks as **transient** settings, which are
dropped on restart — so the block came straight back.

## Fix

1. Reclaimed 5.5 GB (`/root/.cache/pip`, `/root/.npm`, `/root/.rustup`, stale
   `/tmp` dirs) — free space 4.4 GB → 9.9 GB. Not sufficient on its own: 9.9 GB
   of 252 GB is still 96%, above the 95% default.
2. Set the watermarks **persistently and as absolute free-space values**, which
   is the correct form for a quota'd filesystem:
   `low=3gb, high=2gb, flood_stage=1gb`, and cleared `cluster.blocks.create_index`.
3. Cleared `index.blocks.read_only_allow_delete` on all indices → 0 blocked.
4. Re-ran `datahub-upgrade -u RestoreIndices`, which re-reads the aspect store and
   re-emits every MCL: `rowsMigrated=705 … Upgrade RestoreIndices completed with
   result SUCCEEDED` (`00-restore-indices.log`).

After:

```
datasetindex_v2     10
mlmodelindex_v2      2     (1 live + 1 soft-deleted, see below)
datajobindex_v2      3
dataflowindex_v2     1
graph_service_v1   172
```

**No metadata was invented by this recovery.** `RestoreIndices` replays what was
already committed to MySQL; it cannot produce an aspect that was not emitted.

## Consequence for the recorded limitation

An earlier note said the UI graph pane was empty "because DataHub's
graph index lags behind the entity index" and asked for a re-capture. That
explanation was wrong — the graph index was never written. The lineage claims in
D2 stand unchanged, because they were proven against the aspect store, not the
graph. The screenshot note is corrected rather than the lineage.
