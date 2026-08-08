# Substrate ingested, lineage auto-generated

The demonstration data platform, ingested into DataHub, with column-level
lineage produced by dbt ingestion rather than authored by hand.

That distinction is the whole point: a blast radius computed from hand-written
lineage proves nothing about the catalog. These artifacts show the lineage
arriving from the transformation layer itself.

## Captured artifacts

| File | Contents |
|---|---|
| `ingest-postgres.log` | PostgreSQL ingestion run |
| `ingest-dbt.log` | dbt ingestion run — the source of column-level lineage |
| `dbt-run.log` | The `dbt build` that produced the models |
| `01-datasets.json` | Datasets as the catalog sees them |
| `02-upstreamLineage-features.json` | Upstream lineage of the feature table |
| `03-upstreamLineage-dbt-features.json` | The dbt-derived lineage aspect |
| `04-lineage-chain.json` | The traversable chain end to end |
| `ml-model-metadata.json` | Model metadata written by the training run |
| `screenshots/01-lineage.png` | The Lineage tab open on `user_order_features`. The explorer canvas did not render its edges at capture time, so **this image is not the evidence of column-level lineage** — `02-` and `03-` above are. It is kept as the UI capture it is. |
| `screenshots/02-schema.png` | Schema view of `user_order_features`: seven ingested columns and the dbt view definition. Captured on a **clean catalog, before any write-back**, which is why the side panel reads *No tags yet* |

The two screenshots are pre-write-back captures of the ingested substrate. For
the tags and descriptions the Scribe wrote, see
`../proof-pack/d6-loop-pass2/scribe/artifact3-add_tags.json` — the response the
server actually returned.

## The pipeline

```
raw.users · raw.orders  →  stg_users · stg_orders  →  user_order_features  →  churn model
```

Defined in `substrate/`, ingested with the recipes in `recipes/`.
