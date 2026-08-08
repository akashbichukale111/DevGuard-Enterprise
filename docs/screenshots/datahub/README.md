# DataHub screenshots — captured from the running instance

Every image here was produced by
[`scripts/capture_datahub_screenshots.py`](../../../scripts/capture_datahub_screenshots.py)
driving a real browser against the DataHub v1.7.0 instance this project stood up.
Nothing is mocked, drawn, or edited. Re-run the script and they regenerate.

- **Captured** 2026-08-08T11:14:19.019497+00:00
- **Frontend** `http://localhost:9002`
- **Viewport** 1680×1050
- **Probe dataset** `urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_marts.user_order_features,PROD)`
- **Write-back dataset** `urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)`

## The catalog as ingested

| | Shot | What it shows |
|---|---|---|
| `01-login` | [Login](01-login.png) | The DataHub sign-in page this session authenticated against. |
| `02-home` | [Home](02-home.png) | The authenticated landing page: platform tiles and entity counts for the catalog this session ingested. |
| `03-search` | [Search](03-search.png) | Free-text search across all entity types, with the facet rail DataHub builds from the live index. |
| `04-browse` | [Browse](04-browse.png) | Hierarchical browse paths, derived by DataHub from the ingested platform/database/schema structure. |
| `05-dataset` | [Dataset](05-dataset.png) | A dataset entity page: platform, description, owners, tags and domain, all read from the catalog. |
| `06-dataset-schema` | [Dataset schema](06-dataset-schema.png) | The schema DataHub holds for the dataset — field paths, native types and nullability as ingested from Postgres. |
| `07-dataset-lineage` | [Lineage graph](07-dataset-lineage.png) | The lineage graph: stg_users and stg_orders into the feature table, then through the training job to the churn model. Every edge was ingested from the running stack; none was hand-authored. |
| `08-column-lineage` | [Column lineage](08-column-lineage.png) | The same graph with column detail expanded — the field-level mapping DataHub's dbt source derived from the manifest dbt itself produced. |
| `09-impact-analysis` | [Impact analysis](09-impact-analysis.png) | DataHub's own impact analysis over the same graph DevGuard's Pathfinder walks for blast radius. |
| `10-dataset-stats` | [Dataset profile / stats](10-dataset-stats.png) | Dataset profile: row and column counts and per-column statistics, profiled from the live database during ingestion. |
| `11-dataset-quality` | [Assertions / quality](11-dataset-quality.png) | The assertions surface DevGuard's Referee reads for independent recovery corroboration. |
| `12-domains` | [Domains](12-domains.png) | Domains — the governance grouping DevGuard resolves alongside ownership. |
| `13-glossary` | [Business glossary](13-glossary.png) | The business glossary as this instance holds it. |
| `14-tags` | [Tags](14-tags.png) | Tag entities, including `devguard_incident_impacted`. That one is provisioned by scripts/provision_catalog.py, not minted by the agent: `add_tags` fails against a tag URN that does not exist, and the Scribe deliberately does not create one. |
| `15-policies` | [Policies](15-policies.png) | The policy list DevGuard's least-privilege service account is scoped by. |
| `16-ml-model` | [ML model metadata](16-ml-model.png) | The ML model at the end of the blast radius, read as an entity rather than counted as a URN. |
| `17-ingestion` | [Ingestion sources](17-ingestion.png) | DataHub's ingestion surface. The substrate was ingested with the committed recipes rather than through this screen, so this shows the platform capability, not the path used. |
| `18-analytics` | [Analytics](18-analytics.png) | DataHub's built-in usage analytics over this instance. |

## What DevGuard wrote back

The other half of the integration. Everything above is DataHub showing what was
ingested; these are DataHub showing what the agent put there after a verified
recovery — read from the catalog by the same UI a human would use.

| | Shot | What it shows |
|---|---|---|
| `19-writeback-incident` | [Write-back — incident](19-writeback-incident.png) | Artifact 1: the incident DevGuard raised on detection and resolved only after the Referee verified the fix. Filtered to Resolved, because a completed run leaves no active incident behind. |
| `20-writeback-column` | [Write-back — column annotation](20-writeback-column.png) | Artifact 3: the column-level tag and description written back to the exact field the incident was about. |
| `21-writeback-properties` | [Write-back — structured properties](21-writeback-properties.png) | Artifact 4: structured incident facts — verified_at and last_incident_id — written as typed catalog values rather than prose, under the `devguard` namespace. |
| `22-writeback-governance` | [Write-back — governance tab](22-writeback-governance.png) | The governance surface of the dataset the agent wrote to, showing owners, terms and domain alongside the agent's own annotations. |
| `23-writeback-dataset` | [Write-back — dataset overview](23-writeback-dataset.png) | The whole picture on the incident's dataset after a complete run. |

**23/23 captured.**

No page failed to render. A page that could not be shown would be listed here
with its reason rather than quietly omitted — `MANIFEST.json` records the
outcome of every attempt, and a `--only` re-shoot merges into it rather than
replacing it.
