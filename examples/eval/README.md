<!-- GENERATED FILE — DO NOT EDIT.
     Rendered from results.json by scripts/render_eval_readme.py.
     Every number below comes from that file. Re-render after any new run. -->

# Fault-injection evaluation suite

**7 scripted faults**, each really injected into a real PostgreSQL,
each followed by a real `dbt build`, each classified from the real output, each
reverted afterwards. `make eval` runs it.

| | |
|---|---|
| **accuracy** | **7/7 = 100.0%** |
| **false-positive rate** | **0/2 = 0.0%** |
| false negatives | 0 |
| faults producing any runtime signal | 5/7 |

## What this number is a measurement of — read before quoting it

DevGuard's deterministic detection-and-classification path. The Diagnostician could not run in this environment, so this is NOT a measurement of LLM diagnosis.

And a second caveat that matters more than the headline: **7/7 on 7 hand-written
faults, scored by a classifier written in the same repository, is not evidence
that DevGuard diagnoses arbitrary incidents.** It shows that the signature
patterns match the errors these particular faults produce, on this substrate.
The classifier does not know which fault was injected — that part is honest —
but the fault set and the pattern set were written by the same author, and a
100% score on a suite you also wrote is weak evidence by construction.

The two results actually worth attention are further down: the **false-positive
rate**, and what happened to `silent_value_drift`.

## Per-fault results

| fault | category | expected | actual | correct | runtime signal | exit | errored models | failed tests |
|---|---|---|---|---|---|---|---|---|
| `column_rename` | schema | COLUMN_RENAMED | COLUMN_RENAMED | yes | yes | 1 | 1 | 0 |
| `type_change` | schema | TYPE_CHANGED | TYPE_CHANGED | yes | yes | 1 | 1 | 0 |
| `upstream_table_dropped` | schema | UPSTREAM_TABLE_MISSING | UPSTREAM_TABLE_MISSING | yes | yes | 1 | 1 | 0 |
| `permission_revoked` | access | PERMISSION_DENIED | PERMISSION_DENIED | yes | yes | 1 | 5 | 0 |
| `null_rate_spike` | data-quality | NULL_CONSTRAINT_VIOLATED | NULL_CONSTRAINT_VIOLATED | yes | yes | 1 | 1 | 0 |
| `silent_value_drift` | data-quality | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | yes | none | 0 | 0 | 0 |
| `control_no_fault` | control | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | yes | none | 0 | 0 | 0 |

## The two cases that carry the weight

**`control_no_fault`** — the fault-injection evaluation's required control. Real database activity, no real
fault. DevGuard answered `INSUFFICIENT_EVIDENCE`. Any other answer would have
been a false positive, and a system that invents a root cause when nothing is
wrong is worse than one that detects nothing at all.

**`silent_value_drift`** — every `amount_cents` multiplied by 100. Every model
builds, every test passes, and every downstream number — including the ML
model's features — is wrong by two orders of magnitude. DevGuard answered
`INSUFFICIENT_EVIDENCE`, which is **correct and also a real limitation**: it has
no distribution or range check wired in, so it genuinely cannot see this class of
fault. It is counted as correct because refusing is the right behaviour when you
cannot see something; it is emphatically **not** a claim that DevGuard handles
silent data corruption.

2 of 7 faults produced no runtime signal at all. The
suite is deliberately built so that 5/7 are visible and
the rest are not — an eval where everything is detectable measures nothing about
when a system should decline to answer.

## Classifier design

Two rules, both of which cost accuracy and are kept anyway:

* **No failure signal → `INSUFFICIENT_EVIDENCE`.** Never a best guess.
* **A failure it cannot name → `UNKNOWN_FAILURE`**, never a plausible label.
  Guessing between `COLUMN_RENAMED` and `TYPE_CHANGED` on a message supporting
  neither is how an eval scores well and an on-call engineer gets misled.

## Isolation

Faults hit `raw_eval`, a real clone of `raw` built per run; models build into
`analytics_eval*` as the non-superuser role `devguard_eval`; everything is
dropped in a `finally`. The reference-incident substrate is never touched — which is
load-bearing, because the recorded evidence depends on it staying exactly as it is.

The non-superuser role is not decoration either: `REVOKE` against a superuser is
a silent no-op, so `permission_revoked` would have "passed" by never breaking
anything.

## Reproduce

```bash
make eval          # or: DBT_BIN=<dbt> python scripts/run_eval.py
python scripts/render_eval_readme.py
```

Requires the substrate Postgres running (`substrate/docker-compose.yml`). It does
**not** require DataHub, an API key, or the reference-incident to be in any particular
state.

Raw `dbt build` output for every fault is under
`evidence/proof-pack/eval/eval/`, including the green baseline that runs before
any fault is injected — without which a pre-existing failure would be
misattributed to whichever fault happened to be running.
