# Diagnosis, and a refusal demonstrated

Two scenarios, and the second is the one that matters.

**Result: the refusal is demonstrated live, on a genuinely one-sided evidence
chain. The reasoning path is built and unit-tested but has never run against a
live model, and this document does not claim otherwise.**

**Environment:** DataHub Core `v1.6.0` · MCP server `mcp-server-datahub@0.6.0`

## The two runs

| Run | Proof pack | Outcome |
|---|---|---|
| `--scenario full` | `evidence/proof-pack/d5-full/` | Catalog reachable, chain forms, chain is sufficient, Diagnostician invoked |
| `--scenario refusal` | `evidence/proof-pack/d5-refusal/` | Catalog unreachable, chain is `RUNTIME`-only, **`INSUFFICIENT_EVIDENCE`** |

The refusal names the missing evidence class — `DATAHUB_GRAPH` — rather than
returning a generic failure. That is the behaviour the replay UI asserts on in
`make verify-replay-ui`.

## Why the refusal is the headline

An agent that always answers is easy to build and impossible to trust. The
refusal here is not a caught exception dressed up as a decision: the chain
genuinely lacked a required source, the rule detected it structurally, and the
loop stopped rather than producing a plausible root cause from one-sided
evidence.

`tests/test_diagnostician_refusal.py` pins every refusal reason, and
`tests/test_d5_scenarios.py` pins these two recorded outcomes against their
committed packs — so this write-up and the artifacts cannot drift apart.

## What is not proven here

The model's own judgement. `02-groq-egress-probe.txt` records the environment's
egress policy denying `api.groq.com` at CONNECT, so in the `full` scenario the
Diagnostician returns `REASONER_UNAVAILABLE` rather than reasoning. The refusal
path, the chain rule and the evidence typing are all proven; the quality of
model reasoning is not.

## Reproduce

```bash
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> \
  python scripts/run_d5_diagnosis.py --scenario full    --run-id d5-full
python scripts/run_d5_diagnosis.py --scenario refusal --run-id d5-refusal
```
