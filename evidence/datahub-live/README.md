# `datahub-live` — DataHub v1.7.0, stood up and interrogated

Everything in this directory was produced against a DataHub instance provisioned
from **the official `datahub docker quickstart`** during one session, then driven
end to end by DevGuard. It is the newest evidence in the repository and the only
evidence captured against **v1.7.0** with **metadata-service authentication
enforced**.

The older `d0/`–`d10/` directories are not superseded. They were captured
against v1.6.0 and remain the provenance of the committed proof packs and of the
findings in [`docs/upstream/`](../../docs/upstream/). Both generations are pinned
in [`versions.env`](../../versions.env).

## Contents

| File | What it records |
|---|---|
| [`01-service-verification.md`](01-service-verification.md) | Every service endpoint, checked: GMS `/config` and `/health`, GraphQL, the frontend, OpenSearch, the nine Kafka topics, MySQL. Includes the raw `/config` payload the version is read from. |
| [`02-least-privilege-AUTH-OFF.txt`](02-least-privilege-AUTH-OFF.txt) | The security suite run against the **stock quickstart**. ALLOW 4/4, **DENY 0/7**. Kept because the failure is the finding. |
| [`03-least-privilege-AUTH-ON.txt`](03-least-privilege-AUTH-ON.txt) | The same suite with `METADATA_SERVICE_AUTH_ENABLED=true`. ALLOW 5/5, DENY 7/7. |
| [`04-devguard-configuration.md`](04-devguard-configuration.md) | The resolved endpoint table, with *how each row was determined*, and the three-way authentication check. |
| [`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md) | 27 DataHub capabilities, each probed against the live server. |
| `capability-matrix.json` | The same, machine-readable, with **every raw GraphQL response** kept verbatim. |

Regenerate any of it:

```bash
python scripts/verify_datahub_capabilities.py --out evidence/datahub-live
python scripts/verify_least_privilege.py            # refuses if auth is not enforced
python scripts/capture_datahub_screenshots.py --out docs/screenshots/datahub
```

## The capability matrix, and why it has four statuses

A matrix with a "supported" column would be a claim about the DataHub product.
This one is a set of facts about a deployment, so it separates two questions that
a single column conflates:

| Status | Meaning |
|---|---|
| ✅ `VERIFIED` | The field is in the live GraphQL schema **and** this instance returned data for it. |
| 🟡 `PRESENT_NO_DATA` | The field is there and answered cleanly; the catalog holds no such data. A true statement about the catalog, not a defect. |
| ⬜ `ABSENT` | The field is not in this server's introspected schema. Not implemented in this build. |
| ❌ `ERROR` | The query failed. The message is recorded verbatim. |

Result: **25 verified · 2 present-but-empty · 0 absent · 0 error.**

Only the 25 may be described as working. The two empties are named honestly and
are both the same shape of gap — `Freshness / Operations` and `Usage Statistics`
require a source that can read a warehouse's own query history. Snowflake,
BigQuery and Redshift connectors emit it; DataHub's Postgres source does not, so
nothing in this substrate produces one. Filling those panels would mean inventing
numbers.

## Three findings worth reading

**1. Aspects split across DataHub's sibling entities.** DataHub models a physical
table and the dbt node describing it as siblings and merges them in the UI.
GraphQL does not merge them: profiling lands on the warehouse URN, ownership and
assertions on the dbt one. A probe that asked only one side reported
`PRESENT_NO_DATA` for ownership on a catalog that plainly had ownership. The
prober now follows siblings and records **which URN answered** — the `[sibling]`
markers in the matrix.

**2. The quickstart's OpenSearch refuses to create indices on a large disk.**
`system-update` failed with `FORBIDDEN/10/cluster create-index blocked (api)`.
Not a DataHub defect: the disk watermark decider is percentage-based, the
sandbox's filesystem reports 252 GB total with 17 GB free, and 93% used trips the
90% high watermark even though 17 GB is ample for indices measured in hundreds of
megabytes. Fixed by calibrating the watermarks to absolute sizes rather than
disabling the decider — see [`DEPLOYMENT.md`](../../DEPLOYMENT.md).

**3. The quickstart still ships policies unenforced, on v1.7.0.**
`METADATA_SERVICE_AUTH_ENABLED=false` is still the default. The consequence is in
`02-least-privilege-AUTH-OFF.txt`, and it is worse than a test that passes for the
wrong reason: the DENY probes are **mutations**, and with nothing enforcing policy
they do not get refused — they land. That run soft-deleted the hero dataset, added
a cycle to its lineage, and left behind a policy granting the agent
`MANAGE_POLICIES`. All of it was repaired, and
`scripts/verify_least_privilege.py` now refuses to run against an unenforcing
server rather than damaging it.
