# Retrieval ablation

Does retrieving prior runbooks reduce time-to-root-cause? Two arms,
`retrieval=on` and `retrieval=off`, N = 5 each, interleaved.

**Published: [`examples/ablation/`](../../examples/ablation/) — `timings.json`,
a generated `README.md`, and 10 raw run files. Real clocks throughout.**

**What this could not measure, stated plainly:** the effect being ablated is
mediated entirely by the Diagnostician, which could not reach an inference
endpoint in this environment. Both arms therefore derived their root cause the
same deterministic way. **These numbers are the cost of retrieval, not its
benefit.** The benefit is not null — it is *unmeasured*, because the component
that consumes retrieved knowledge was switched off by the environment rather
than by the experiment.

`comparison.retrieval_could_affect_root_cause` is `false` in `timings.json` for
exactly this reason. The harness is complete; the same command on a machine with
a working key produces the comparison the design asks for.

## Results

| arm | n | TTRC median | post-detection median | MCP calls | docs |
|---|---|---|---|---|---|
| `retrieval=on` | 5 | 5.14 s | 1.98 s | 8 | 5 |
| `retrieval=off` | 5 | 4.87 s | 1.85 s | 6 | 0 |

Two timings are published because either alone misleads. Time-to-root-cause
includes detection — a real `dbt run` that varies by more than the effect being
measured — and the arms' ranges overlap heavily, so **the TTRC delta is not
distinguishable from noise.** The signal is in post-detection time: retrieval
costs **+0.128 s** and **+2 MCP calls**, adding 4 evidence items.

N = 5 per arm, one machine, one incident, one substrate. These figures
characterise this setup and do not generalise.

## Proof packs

`evidence/proof-pack/ablation/` — one pack per run, 10 in total.

## Reproduce

```bash
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> python scripts/run_ablation.py -n 5
python scripts/render_ablation_readme.py
```

The substrate must be in the broken demo state first
(`python scripts/reset_demo.py`). The runner exercises the **read** side only,
because remediation and write-back would add minutes of noise per run and would
mutate the shared catalog ten times over.
