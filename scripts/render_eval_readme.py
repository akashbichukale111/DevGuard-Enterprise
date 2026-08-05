#!/usr/bin/env python
"""
render_eval_readme.py — the fault-injection evaluation's README table, rendered from results.json (the generated-numbers rule).

    python scripts/render_eval_readme.py          # write
    python scripts/render_eval_readme.py --check  # CI: assert up to date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS = Path("examples/eval/results.json")
README = Path("examples/eval/README.md")


def render(data: dict) -> str:
    rows = data["results"]
    by_name = {r["fault"]: r for r in rows}

    def row(r: dict) -> str:
        mark = "yes" if r["correct"] else "**NO**"
        signal = "yes" if r["failure_observed"] else "none"
        return (f"| `{r['fault']}` | {r['category']} | {r['expected']} | "
                f"{r['actual']} | {mark} | {signal} | "
                f"{r['exit_code']} | {r['errored_models']} | {r['failed_tests']} |")

    table = "\n".join(row(r) for r in rows)
    silent = [r for r in rows if not r["failure_observed"]]
    detected = data["detected_total"]

    return f"""<!-- GENERATED FILE — DO NOT EDIT.
     Rendered from results.json by scripts/render_eval_readme.py.
     Every number below comes from that file. Re-render after any new run. -->

# Fault-injection evaluation suite

**{data['total']} scripted faults**, each really injected into a real PostgreSQL,
each followed by a real `dbt build`, each classified from the real output, each
reverted afterwards. `make eval` runs it.

| | |
|---|---|
| **accuracy** | **{data['correct']}/{data['total']} = {data['accuracy']:.1%}** |
| **false-positive rate** | **{data['false_positives']}/{data['control_total']} = {data['false_positive_rate']:.1%}** |
| false negatives | {data['false_negatives']} |
| faults producing any runtime signal | {detected}/{data['total']} |

## What this number is a measurement of — read before quoting it

{data['measures']}

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
{table}

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

{len(silent)} of {data['total']} faults produced no runtime signal at all. The
suite is deliberately built so that {detected}/{data['total']} are visible and
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
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    results_path, readme_path = repo / RESULTS, repo / README
    if not results_path.exists():
        print(f"{RESULTS} not found — run scripts/run_eval.py first", file=sys.stderr)
        return 2

    rendered = render(json.loads(results_path.read_text()))
    if args.check:
        current = readme_path.read_text() if readme_path.exists() else ""
        if current != rendered:
            print(f"{README} is out of date with {RESULTS}. "
                  f"Run: python scripts/render_eval_readme.py", file=sys.stderr)
            return 1
        print(f"{README} matches {RESULTS}")
        return 0

    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(rendered)
    print(f"wrote {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
