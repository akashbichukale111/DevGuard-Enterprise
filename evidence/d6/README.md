# The remediation loop closes, twice, from clean state

The full incident loop — proposal, screening, validation, risk classification,
owner approval, remediation, recovery verification, five-artifact write-back,
and retrieval on the next run — executed end to end against the live stack.

**Result: the remediation and retrieval sides run end to end, twice from clean
state, with all five write-back artifacts landing and the incident resolved in
DataHub's own UI. The Diagnostician's reasoning step remains blocked by the
environment's egress policy, so the *complete* loop including model reasoning is
not claimed.**

**Environment:** DataHub Core `v1.6.0` · MCP server `mcp-server-datahub@0.6.0`

## A pass, in full

```
$ python scripts/reset_demo.py && python scripts/run_d6_loop.py --run-id d6-loop-pass1
[watcher]       exit=1 model=stg_users column=user_id
[probe]         raw.users: ('customer_id', 'email', 'country', 'signup_ts', 'is_active')
[archivist]     OK: PREVIOUS VERIFIED INCIDENT — 3 document(s) retrieved.
[pathfinder]    7 impacted, reaches_ml_model=True
[diagnostician] REASONER_UNAVAILABLE (is_refusal=False)
[surgeon]       OK: customer_id
[sentinel]      patch risk=LOW blocked=False
[referee]       validation passed=True (isolated schema analytics_devguard_check)
[magistrate]    risk=LOW mode=NAMED_OWNER owners=['DataHub']
[human]         approved by DataHub Admin (local operator)
```

Note `[archivist] PREVIOUS VERIFIED INCIDENT — 3 document(s) retrieved` on the
second pass. That is the loop actually closing: knowledge written back by the
first run is retrieved by the next one. Without it, this would be a pipeline
that happens to run twice.

## Recorded runs

| Proof pack | What it shows |
|---|---|
| `d6-loop-pass1` | First clean-state pass |
| `d6-loop-pass2` | Second pass, retrieving what the first wrote |
| `d6-dry-run` | Every write payload recorded, nothing sent |
| `d6-fail-the-fix` | Validation fails — no human is asked, nothing is written |

`d6-fail-the-fix` is the one worth opening. A proposed fix that does not work
costs nothing and changes nothing, because validation runs against a throwaway
schema from a copy of the project *before* anyone is asked to approve it.

## Reproduce

```bash
python scripts/reset_demo.py
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> \
  python scripts/run_d6_loop.py --run-id d6-loop-pass1
```
