# The evidence chain forms

The read side of the incident loop, run against the live stack, producing a
typed evidence chain sufficient to support a root cause.

**Result: the chain forms on real evidence and is sufficient under the evidence
rule.**

```
evidence items      : 12
sources             : ['DATAHUB_GRAPH', 'RUNTIME']
chain digest        : a16d2927e4e56487
CHAIN IS SUFFICIENT : True
```

Both required sources are present. `RUNTIME` alone would be an error message;
`DATAHUB_GRAPH` alone would be a theory. The chain is an explanation because it
carries both.

## The agents involved

| Step | Agent | Module | Kind |
|---|---|---|---|
| Detect failure | Watcher | `backend/v2/agents/watcher.py` | deterministic |
| Negotiate capabilities | Archivist | `backend/v2/agents/archivist.py` | tools |
| Resolve graph context | Cartographer | `backend/v2/agents/cartographer.py` | tools |
| Blast radius | Pathfinder | `backend/v2/agents/pathfinder.py` | tools |
| Screen untrusted text | Sentinel | `backend/v2/sentinel.py` | deterministic |
| Retrieve prior knowledge | Archivist | (as above) | tools |

## Proof pack

`evidence/proof-pack/d4-evidence-chain/` — 13 artifacts, every one redacted at
capture time.

## Reproduce

```bash
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> \
  python scripts/run_d4_evidence_chain.py --run-id d4-evidence-chain
```
