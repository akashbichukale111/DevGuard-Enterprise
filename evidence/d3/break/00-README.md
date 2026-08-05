# The breaking change, and what actually happened

**Executed:** 2026-08-01T02:16:51Z · `ALTER TABLE raw.users RENAME COLUMN user_id TO customer_id;`

The design predicts: *"REAL mutation … REAL failure: dbt model + reporting query
+ feature job break."* One of those three broke. The other two did something
worse, and this file says so rather than rounding it up to a clean outage.

## What broke

**dbt: hard failure, exit code 1.** (`03-AFTER-dbt-failure.txt`)

```
[ERROR]: in model stg_users (models/staging/stg_users.sql)
  Database Error in model stg_users
  column "user_id" does not exist
  LINE 13:     user_id,
Done. PASS=1 WARN=0 ERROR=1 SKIP=1 NO-OP=0 REUSED=0 TOTAL=3
```

`stg_users` errors, and `user_order_features` — the ML feature table — is
**SKIPped**, so it is never rebuilt.

## What did NOT break, and why that is the real story

**The reporting view kept working.** `SELECT count(*) FROM analytics_staging.stg_users`
still returns **1715**. PostgreSQL binds view definitions to column *attribute
numbers*, not names, so the rename propagated into the already-deployed view and
Postgres silently rewrote it:

```sql
$ SELECT pg_get_viewdef('analytics_staging.stg_users'::regclass, true);
  SELECT customer_id AS user_id,      -- <— Postgres rewrote this itself
     email, country, signup_ts, is_active
    FROM raw.users
   WHERE is_active;
```

**The ML feature job kept working too.** (`04-AFTER-ml-outcome.txt`)

```
trained on 1715 rows from analytics_marts.user_order_features
test accuracy: 0.7995
train exit code: 0
```

It trains, it reports the same accuracy, and it exits 0 — because the mart it
reads was SKIPped, not dropped, so it is a **stale table that still has a
`user_id` column**.

## Why this is a better reference-incident than the one the incident loop assumed

The predicted failure is an outage: something goes red, somebody notices. What
this substrate actually produced is **drift** — the deployed artifacts and the
source of truth have diverged, and *nothing downstream complains*:

| Layer | State after the rename | Complains? |
|---|---|---|
| `raw.users` | column is now `customer_id` | — |
| `analytics_staging.stg_users` (deployed view) | auto-rewritten to `customer_id AS user_id` | **no** |
| `analytics_staging.stg_users` (dbt model in git) | references `user_id`, no longer valid | only on rebuild |
| `analytics_marts.user_order_features` | stale, never rebuilt, still has `user_id` | **no** |
| churn model | retrains on stale data, same 0.7995 | **no** |
| DataHub catalog | still describes `raw.users.user_id` | **no** |

The only signal in the entire stack is the dbt exit code. Everything downstream
of it reports success on data that no longer reflects its source. A model that
fails loudly gets fixed; a model that keeps scoring 0.7995 off a frozen table
does not.

**This is the claim DevGuard is actually defending**, and it is stronger than the
one in the contract — so the loop's "feature job breaks" line is wrong for this
substrate and is not repeated anywhere in our evidence. What breaks is the
rebuild path; what rots is everything else.

## Files

| File | What |
|---|---|
| `01-BEFORE-green.txt` | dbt PASS=3, model trains, accuracy 0.7995 |
| `02-THE-BREAK.txt` | the ALTER TABLE and the resulting `\d raw.users` |
| `03-AFTER-dbt-failure.txt` | the real dbt error, exit code 1 |
| `04-AFTER-ml-outcome.txt` | the ML job succeeding on stale data |
| `05-AFTER-substrate-state.txt` | view still serving 1715 rows; mart frozen at 1715 |

## One more artefact of this, visible in git

The post-break training run overwrote `substrate/ml/artifacts/model_metadata.json`.
The only field that changed is the timestamp:

```diff
-  "trained_at": "2026-07-31T13:13:46.002949+00:00",
+  "trained_at": "2026-08-01T02:17:19.045123+00:00",
```

Same 1715 rows, same 0.7995, same feature columns — a fresh training run that
produced a byte-identical model from a table that stopped tracking its source
half an hour earlier.

That change is committed on purpose. It also means the registered `mlModel` in
DataHub now carries `trained_at = 2026-07-31T13:13:46` while the artefact on disk
says `2026-08-01T02:17:19`. **That divergence is not an error to clean up** — it
is the catalog being stale in exactly the way the rest of the stack is stale, and
reconciling it is remediation work, not evidence-keeping.
