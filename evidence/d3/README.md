# ML model registered, queries verified, loop broken

Three things, all against the live stack: register the ML model with lineage a
blast radius can traverse, verify real query usage, and execute the breaking
change so the incident is genuine.

**Result: all three done. One came out differently from what was expected, and
that difference is documented rather than smoothed over.**

**Environment:** DataHub Core `v1.6.0` · MCP server `mcp-server-datahub@0.6.0`

## The model

| | |
|---|---|
| URN | `urn:li:mlModel:(urn:li:dataPlatform:devguard_ml,devguard_churn_risk,PROD)` |
| Platform | `devguard_ml` — a custom platform, deliberately **not** `mlflow`. MLflow is not in this stack, and an mlflow URN would claim a tool that is not running. |
| Training job | `urn:li:dataJob:(urn:li:dataFlow:(devguard_ml,substrate_ml,PROD),train_churn_model)` |
| Properties | Read from `substrate/ml/artifacts/model_metadata.json`, written by the training run |

## The traversal finding

`mlModelTrainingData` — the aspect that *declares* a model's training data —
produces **no graph edge**. A blast radius cannot reach a model through it. The
traversable path had to be established differently, and
`backend/v2/agents/pathfinder.py` documents the working route at the line that
depends on it. `06-graphql-npe-proof.txt` records the server behaviour.

## Captured artifacts

| File | Contents |
|---|---|
| `01-mlmodel-entity.json` | The registered model |
| `01b-mlmodeltrainingdata-raw-aspect.json` | The raw aspect, showing why no edge appears |
| `02-lineage-featuretable-to-model.json` | The traversable path |
| `03-blast-radius-raw-users-BEFORE-break.json` | Blast radius before the break |
| `04-get_dataset_queries.json` | Real SQL usage of the dataset |
| `05-blast-radius-mcp.json` | Blast radius over MCP |
| `06-graphql-npe-proof.txt` | Server-side behaviour under the failing traversal |
| `break/` | The executed breaking change |
| `writeback/` | Write-back verification |
| `00-opensearch-flood-stage-diagnosis.md` | Search-index outage found and fixed while verifying this |
| `00-restore-indices.log` | The recovery |

## The index outage

Worth reading if search results ever look impossibly empty: every ingestion had
been reaching MySQL and never reaching OpenSearch, because all 82 indices
carried a flood-stage `read_only_allow_delete` block. Entity counts before and
after the fix:

```
before:  datasetindex_v2  1   mlmodelindex_v2 0   graph_service_v1  20
after:   datasetindex_v2 10   mlmodelindex_v2 1   graph_service_v1 172
```

Nothing was wrong with the writes. Everything was wrong with what could be found.
