# Fault-injection evaluation suite

Seven scripted faults, each really injected into a real PostgreSQL, each
followed by a real `dbt build`, each classified from real output, each reverted
afterwards.

**Published: [`examples/eval/`](../../examples/eval/) — `results.json` and a
generated `README.md`. `make eval` runs it.**

| | |
|---|---|
| Accuracy | 7/7 = 100.0% |
| False-positive rate | 0/2 = 0.0% |
| False negatives | 0 |
| Faults producing any runtime signal | 5/7 |

## Read the headline number with its caveat

7/7 on 7 hand-written faults, scored by a classifier written in the same
repository, is **not** evidence that the system diagnoses arbitrary incidents.
The fault set and the pattern set share an author, and a perfect score on a
suite you also wrote is weak evidence by construction.

The two results that carry real weight:

- **`control_no_fault`** — real database activity, no real fault. The answer was
  `INSUFFICIENT_EVIDENCE`. Any other answer would have been a false positive,
  and a system that invents a root cause when nothing is wrong is worse than one
  that detects nothing.
- **`silent_value_drift`** — every `amount_cents` multiplied by 100. Every model
  builds, every test passes, and every downstream number is wrong by two orders
  of magnitude. The answer was `INSUFFICIENT_EVIDENCE`, which is correct *and* a
  real limitation: there is no distribution or range check, so this fault class
  is genuinely invisible.

Two of seven faults produce no runtime signal at all, by design. An evaluation
where everything is detectable measures nothing about when a system should
decline to answer.

## Isolation

Faults hit `raw_eval`, a real clone of `raw` built per run. Models build into
`analytics_eval*` as the non-superuser role `devguard_eval`, and everything is
dropped in a `finally`. The demo substrate is never touched.

The non-superuser role is not decoration: `REVOKE` against a superuser is a
silent no-op, so `permission_revoked` would have "passed" by never breaking
anything.

## Proof packs

`evidence/proof-pack/eval/` — raw `dbt build` output for every fault, including
the green baseline that runs before any injection. Without that baseline a
pre-existing failure would be misattributed to whichever fault happened to be
running.

## Reproduce

```bash
make eval          # or: DBT_BIN=<dbt> python scripts/run_eval.py
python scripts/render_eval_readme.py
```

Requires the substrate PostgreSQL (`substrate/docker-compose.yml`). Does **not**
require DataHub, an API key, or the demo loop to be in any particular state.
