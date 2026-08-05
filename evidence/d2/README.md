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
| `screenshots/01-lineage.png` | Column-level lineage in the DataHub UI |
| `screenshots/02-schema.png` | Schema view |

## The pipeline

```
raw.users · raw.orders  →  stg_users · stg_orders  →  user_order_features  →  churn model
```

Defined in `substrate/`, ingested with the recipes in `recipes/`.
