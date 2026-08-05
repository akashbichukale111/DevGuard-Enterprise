#!/usr/bin/env python
"""
render_ablation_readme.py — Rule: numbers are generated, not typed.

Rule: *"`timings.json` is the single source; README, DEMO_RUNBOOK, description
and `examples/` numbers are **templated from it**. CI asserts only that rendered
docs match the source (a one-line diff check)."*

So this renders `examples/ablation/README.md` from
`examples/ablation/timings.json`. Every figure in that document comes from this
script reading that file. Nothing is hand-written, which is the only way the measured-numbers rule
("a number typed by hand is the no-fabrication rule violation") can actually hold once a
document has a dozen figures in it.

    python scripts/render_ablation_readme.py          # write
    python scripts/render_ablation_readme.py --check  # CI: assert up to date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TIMINGS = Path("examples/ablation/timings.json")
README = Path("examples/ablation/README.md")


def render(timings: dict) -> str:
    on = timings["arms"]["on"]
    off = timings["arms"]["off"]
    cmp_ = timings["comparison"]
    runs = timings["runs"]
    cost = timings["cost_model"]

    def row(s: dict) -> str:
        return (f"| `retrieval={s['arm']}` | {s['n']} | "
                f"**{s['ttrc_median_s']:.2f}** | {s['ttrc_min_s']:.2f} | {s['ttrc_max_s']:.2f} | "
                f"**{s['post_detection_median_s']:.2f}** | "
                f"{s['post_detection_min_s']:.2f} | {s['post_detection_max_s']:.2f} | "
                f"{s['mcp_tool_calls_median']:.0f} | {s['documents_retrieved_median']:.0f} | "
                f"{s['tokens_total']} | ${s['usd_cost_total']:.2f} |")

    per_run = "\n".join(
        f"| {r['arm']} | {r['run_index']} | {r['time_to_root_cause_s']:.2f} | "
        f"{r['detection_s']:.2f} | {r['post_detection_s']:.2f} | "
        f"{r['mcp_tool_calls']} | {r['documents_retrieved']} | "
        f"{r['evidence_items']} | {r['root_cause_source']} |"
        for r in sorted(runs, key=lambda r: (r["arm"], r["run_index"])))

    could = cmp_["retrieval_could_affect_root_cause"]
    verdicts = sorted({r["diagnostician_verdict"] for r in runs})

    return f"""<!-- GENERATED FILE — DO NOT EDIT.
     Rendered from timings.json by scripts/render_ablation_readme.py.
     Every number below comes from that file. Re-render after any new run. -->

# Ablation — does retrieving prior runbooks reduce time-to-root-cause?

**Retrieval ablation.** Same incident, same asset. Two arms, `retrieval=on` and `retrieval=off`,
**N = {on['n']} per arm**, interleaved so machine load is shared between them
rather than landing on whichever ran last.

## Read this before the table

The effect this ablation is designed to measure is mediated entirely by the
**Diagnostician**: retrieved runbooks enter its prompt, and a better prompt
should shorten its reasoning. In this environment the Diagnostician **cannot
run** — `api.groq.com` is denied at CONNECT by the egress policy — and every run
below reports `{', '.join(verdicts)}`.

Both arms therefore produced the root cause the *same* way: derived
deterministically from runtime evidence. **What follows measures the cost of
retrieval, not its benefit.** The benefit side is not null — it is *unmeasured*,
because the component that would consume the retrieved knowledge was switched
off by the environment, not by the experiment.

`comparison.retrieval_could_affect_root_cause` is **{str(could).lower()}** in
`timings.json` for exactly this reason. The harness is complete; on a machine
with a working key the same command produces the comparison the retrieval ablation asks for.

## Results

| arm | n | TTRC median (s) | min | max | post-detection median (s) | min | max | MCP calls | docs | tokens | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
{row(on)}
{row(off)}

**Deltas (on − off), medians:**

| metric | delta |
|---|---|
| time-to-root-cause | {cmp_['delta_ttrc_median_s']:+.4f} s |
| post-detection | {cmp_['delta_post_detection_median_s']:+.4f} s |
| MCP tool calls | {cmp_['delta_mcp_tool_calls_median']:+.0f} |
| evidence items | {cmp_['delta_evidence_items_median']:+.0f} |

## What these numbers actually say

**Two timings are published because either alone misleads.**
`time-to-root-cause` includes detection — a real `dbt run` against the real
substrate, which takes seconds and varies by more than the effect being
measured. The TTRC min/max ranges of the two arms overlap heavily, so **the TTRC
delta of {cmp_['delta_ttrc_median_s']:+.4f}s is not distinguishable from noise**
and should not be quoted as a result. This is the same reason the retrieval ablation rejects MTTR
in favour of time-to-root-cause: a large, noisy component hides the effect.

`post-detection` — from "failure observed" to "root cause available" — is where
the signal is. There, retrieval costs
**{cmp_['delta_post_detection_median_s']:+.4f}s** and
**{cmp_['delta_mcp_tool_calls_median']:+.0f} extra MCP calls**, and adds
{cmp_['delta_evidence_items_median']:+.0f} evidence items to the chain.

**Sample size and its limits, in one honest sentence** (the retrieval ablation asks for exactly
this): N = {on['n']} per arm on a single machine against a single incident on a
single substrate, so these figures characterise this setup only — they are a
measurement of overhead, not an estimate of retrieval's value, and they do not
generalise to other incidents, catalogs or hardware.

## Cost accounting (the cost-accounting requirement)

| | on | off |
|---|---|---|
| model calls | {on['model_calls_total']} | {off['model_calls_total']} |
| tokens | {on['tokens_total']} | {off['tokens_total']} |
| USD | ${on['usd_cost_total']:.2f} | ${off['usd_cost_total']:.2f} |

Zero across the board, and the reason matters: **{cost['note']}**

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
{per_run}

## Reproduce

```bash
DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> python scripts/run_ablation.py -n {on['n']}
python scripts/render_ablation_readme.py
```

The substrate must be in the reference-incident broken state first
(`python scripts/reset_demo.py`). The runner exercises the **read** side only —
steps 2–9 — because remediation and write-back would add minutes of noise per
run and would mutate the shared catalog ten times over.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if README.md is stale (for CI)")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    timings_path, readme_path = repo / TIMINGS, repo / README
    if not timings_path.exists():
        print(f"{TIMINGS} not found — run scripts/run_ablation.py first",
              file=sys.stderr)
        return 2

    rendered = render(json.loads(timings_path.read_text()))
    if args.check:
        current = readme_path.read_text() if readme_path.exists() else ""
        if current != rendered:
            print(f"{README} is out of date with {TIMINGS}. "
                  f"Run: python scripts/render_ablation_readme.py", file=sys.stderr)
            return 1
        print(f"{README} matches {TIMINGS}")
        return 0

    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(rendered)
    print(f"wrote {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
