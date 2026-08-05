<!-- GENERATED FILE — DO NOT EDIT.
     Rendered from timings.json by scripts/render_ablation_readme.py.
     Every number below comes from that file. Re-render after any new run. -->

# Ablation — does retrieving prior runbooks reduce time-to-root-cause?

**Retrieval ablation.** Same incident, same asset. Two arms, `retrieval=on` and `retrieval=off`,
**N = 5 per arm**, interleaved so machine load is shared between them
rather than landing on whichever ran last.

## Read this before the table

The effect this ablation is designed to measure is mediated entirely by the
**Diagnostician**: retrieved runbooks enter its prompt, and a better prompt
should shorten its reasoning. In this environment the Diagnostician **cannot
run** — `api.groq.com` is denied at CONNECT by the egress policy — and every run
below reports `REASONER_UNAVAILABLE`.

Both arms therefore produced the root cause the *same* way: derived
deterministically from runtime evidence. **What follows measures the cost of
retrieval, not its benefit.** The benefit side is not null — it is *unmeasured*,
because the component that would consume the retrieved knowledge was switched
off by the environment, not by the experiment.

`comparison.retrieval_could_affect_root_cause` is **false** in
`timings.json` for exactly this reason. The harness is complete; on a machine
with a working key the same command produces the comparison the retrieval ablation asks for.

## Results

| arm | n | TTRC median (s) | min | max | post-detection median (s) | min | max | MCP calls | docs | tokens | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `retrieval=on` | 5 | **5.14** | 4.89 | 6.56 | **1.98** | 1.84 | 2.09 | 8 | 5 | 0 | $0.00 |
| `retrieval=off` | 5 | **4.87** | 4.72 | 6.39 | **1.85** | 1.76 | 2.99 | 6 | 0 | 0 | $0.00 |

**Deltas (on − off), medians:**

| metric | delta |
|---|---|
| time-to-root-cause | +0.2690 s |
| post-detection | +0.1279 s |
| MCP tool calls | +2 |
| evidence items | +4 |

## What these numbers actually say

**Two timings are published because either alone misleads.**
`time-to-root-cause` includes detection — a real `dbt run` against the real
substrate, which takes seconds and varies by more than the effect being
measured. The TTRC min/max ranges of the two arms overlap heavily, so **the TTRC
delta of +0.2690s is not distinguishable from noise**
and should not be quoted as a result. This is the same reason the retrieval ablation rejects MTTR
in favour of time-to-root-cause: a large, noisy component hides the effect.

`post-detection` — from "failure observed" to "root cause available" — is where
the signal is. There, retrieval costs
**+0.1279s** and
**+2 extra MCP calls**, and adds
+4 evidence items to the chain.

**Sample size and its limits, in one honest sentence** (the retrieval ablation asks for exactly
this): N = 5 per arm on a single machine against a single incident on a
single substrate, so these figures characterise this setup only — they are a
measurement of overhead, not an estimate of retrieval's value, and they do not
generalise to other incidents, catalogs or hardware.

## Cost accounting (the cost-accounting requirement)

| | on | off |
|---|---|---|
| model calls | 0 | 0 |
| tokens | 0 | 0 |
| USD | $0.00 | $0.00 |

Zero across the board, and the reason matters: **Zero because zero model calls were made in this environment, not because inference is free. Populate from the provider's price list when a key is available.**

DevGuard's deterministic agents — Watcher, Sentinel, Referee, Magistrate,
Surgeon, Scribe — genuinely cost nothing per incident beyond wall-clock time,
because they use no model at all. That is a real property of the design, not an
artefact of the blocker. The one agent that *would* cost money, the
Diagnostician, is the one that could not run, so **the interesting half of the
cost question is unanswered.**

## Per-run raw data

Raw JSON per run is in [`raw/`](raw/), and each run's full proof pack is under
`evidence/proof-pack/ablation/`.

| arm | run | TTRC (s) | detection (s) | post-detection (s) | MCP calls | docs | evidence | root cause |
|---|---|---|---|---|---|---|---|---|
| off | 1 | 6.39 | 3.40 | 2.99 | 6 | 0 | 14 | DERIVED |
| off | 2 | 4.78 | 2.92 | 1.86 | 6 | 0 | 14 | DERIVED |
| off | 3 | 4.87 | 3.02 | 1.85 | 6 | 0 | 14 | DERIVED |
| off | 4 | 4.72 | 2.96 | 1.76 | 6 | 0 | 14 | DERIVED |
| off | 5 | 4.95 | 3.13 | 1.81 | 6 | 0 | 14 | DERIVED |
| on | 1 | 4.99 | 2.95 | 2.04 | 8 | 5 | 18 | DERIVED |
| on | 2 | 6.56 | 4.58 | 1.98 | 8 | 5 | 18 | DERIVED |
| on | 3 | 4.89 | 3.05 | 1.84 | 8 | 5 | 18 | DERIVED |
| on | 4 | 5.40 | 3.32 | 2.09 | 8 | 5 | 18 | DERIVED |
| on | 5 | 5.14 | 3.19 | 1.96 | 8 | 5 | 18 | DERIVED |

## Reproduce

```bash
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> python scripts/run_ablation.py -n 5
python scripts/render_ablation_readme.py
```

The substrate must be in the reference-incident broken state first
(`python scripts/reset_demo.py`). The runner exercises the **read** side only —
steps 2–9 — because remediation and write-back would add minutes of noise per
run and would mutate the shared catalog ten times over.
