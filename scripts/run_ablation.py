#!/usr/bin/env python
"""
run_ablation.py — the retrieval ablation. Two arms, N>=5 each, published to examples/ablation/.

    DATAHUB_TOKEN_FILE=<t> DBT_BIN=<dbt> python scripts/run_ablation.py -n 5

Measures time-to-root-cause with `retrieval=on` and `retrieval=off`, plus MCP
tool calls, tokens and cost per arm. Every number lands in
`examples/ablation/timings.json`, which is the single source the generated-numbers rule requires — the
published table is rendered from it, never typed.

WHAT THIS CAN AND CANNOT SHOW IN THIS ENVIRONMENT

Retrieval's effect on time-to-root-cause is mediated by the Diagnostician: prior
runbooks enter its prompt and should shorten its reasoning. `api.groq.com` is
denied at CONNECT here, so the Diagnostician does not reason and the root cause
is derived deterministically from runtime evidence — **identically in both
arms**.

So this run measures the **cost** of retrieval (an extra MCP round trip, extra
evidence items, extra milliseconds) and cannot measure its **benefit**. That is
a fact about the environment, not a finding about retrieval, and
`comparison.retrieval_could_affect_root_cause` records it as `false` so no
reader can mistake one for the other.

The harness is complete. With a working key the same command produces the
comparison the retrieval ablation actually asks for.

This deliberately runs only the READ side of the loop (steps 2–9): the ablation
is about time-to-root-cause, and remediation and write-back would add minutes of
noise while changing the shared catalog on every one of ten runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.ablation import (
    ArmSummary, RunMetrics, Stopwatch, compare, usd_cost, write_timings,
)
from backend.v2.agents.archivist import Archivist
from backend.v2.agents.cartographer import Cartographer
from backend.v2.agents.diagnostician import Diagnostician, GroqReasoner, Verdict
from backend.v2.agents.pathfinder import Pathfinder
from backend.v2.agents.watcher import ColumnProbe, RuntimeEvidenceProvider, Watcher
from backend.v2.datahub_client import DataHubMCPClient, load_token
from backend.v2.evidence import (
    EvidenceBuilder, EvidenceChain, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import Sentinel

RAW_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
FEATURES = ("urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "devguard.analytics_marts.user_order_features,PROD)")


def one_run(*, arm: str, index: int, token: str, dbt_dir: Path, dbt_bin: str,
            pack_root: str) -> RunMetrics:
    retrieval = arm == "on"
    incident_id = f"AB{arm.upper()}{index:02d}"
    pack = ProofPack(pack_root, f"ablation-{arm}-{index:02d}")
    builder = EvidenceBuilder(incident_id)
    chain = EvidenceChain(incident_id=incident_id)
    sentinel = Sentinel()
    watch = Stopwatch()
    tool_calls = 0
    documents = 0

    # ---- detection ---------------------------------------------------------
    env = dict(os.environ, DBT_PROFILES_DIR=".")
    failure = RuntimeEvidenceProvider(cwd=dbt_dir).observe([dbt_bin, "run"], env=env)
    items, _ = Watcher(builder, pack).run(failure)
    chain = chain.add(*items)
    detection_s = watch.mark("detection")

    live = ColumnProbe().probe("raw", "users")
    chain = chain.add(builder.make(
        source=EvidenceSource.RUNTIME, trust=EvidenceTrust.TRUSTED_SYSTEM,
        confidence=EvidenceConfidence.OBSERVED,
        claim=f"{live.table} now has columns: {', '.join(live.columns)}",
        raw_ref=pack.write("watcher/column-probe.json",
                           {"columns": list(live.columns)},
                           note="Live information_schema.")))
    watch.mark("column_probe")

    with DataHubMCPClient(token=token) as client:
        archivist = Archivist(client, builder, pack, sentinel, retrieval=retrieval)
        knowledge, arch_ev, arch_handoff = archivist.run(
            failure.missing_column or "schema drift")
        chain = chain.add(*arch_ev)
        tool_calls += len(arch_handoff.tool_calls)
        documents = len(knowledge.documents)
        watch.mark("archivist", len(arch_handoff.tool_calls))

        cartographer = Cartographer(client, builder, pack, sentinel)
        term = (failure.failing_model or "stg_users").split(".")[-1]
        _, cart_ev, cart_handoff = cartographer.run(term, prefer_platform="postgres")
        chain = chain.add(*cart_ev)
        tool_calls += len(cart_handoff.tool_calls)
        watch.mark("cartographer", len(cart_handoff.tool_calls))

        pathfinder = Pathfinder(client, builder, pack)
        _, path_ev, path_handoff = pathfinder.run(
            RAW_USERS, column=failure.missing_column, terminal_urn=FEATURES, max_hops=5)
        chain = chain.add(*path_ev)
        tool_calls += len(path_handoff.tool_calls)
        watch.mark("pathfinder", len(path_handoff.tool_calls))

        reasoner = GroqReasoner() if os.environ.get("GROQ_API_KEY") else None
        diagnosis, diag_handoff = asyncio.run(
            Diagnostician(reasoner=reasoner, pack=pack, sentinel=sentinel).run(chain))
        watch.mark("diagnostician")

    # The root cause is available at this point either way — from the model if
    # one ran, otherwise derived from runtime evidence. Both are recorded with
    # their provenance so no reader has to guess which happened.
    model_produced = diagnosis.verdict is Verdict.ROOT_CAUSE_IDENTIFIED
    ttrc = watch.since_start()
    post_detection = ttrc - detection_s

    metrics = RunMetrics(
        arm=arm, run_index=index, incident_id=incident_id,
        time_to_root_cause_s=round(ttrc, 4),
        post_detection_s=round(post_detection, 4),
        detection_s=round(detection_s, 4),
        mcp_tool_calls=tool_calls,
        evidence_items=len(chain.items),
        documents_retrieved=documents,
        tokens_prompt=0, tokens_completion=diagnosis.tokens,
        model_calls=1 if model_produced else 0,
        usd_cost=usd_cost(0, diagnosis.tokens),
        root_cause_source="MODEL" if model_produced else "DERIVED",
        diagnostician_verdict=diagnosis.verdict.value,
        chain_is_sufficient=chain.is_sufficient,
        phases=watch.phases,
        proof_pack=str(pack.root),
    )
    pack.write("run-metrics.json", metrics.to_dict(), note="The retrieval ablation metrics for this run.")
    pack.write_index({"arm": arm, "run_index": index,
                      "time_to_root_cause_s": metrics.time_to_root_cause_s})
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--runs", type=int, default=5,
                        help="runs per arm (the retrieval ablation requires >= 5)")
    parser.add_argument("--out", default="examples/ablation")
    parser.add_argument("--pack-root", default="evidence/proof-pack/ablation")
    parser.add_argument("--dbt-dir", default="substrate/dbt")
    parser.add_argument("--dbt-bin", default=os.environ.get("DBT_BIN", "dbt"))
    args = parser.parse_args()

    if args.runs < 5:
        print("the retrieval ablation requires N >= 5 per arm.", file=sys.stderr)
        return 2
    if not shutil.which(args.dbt_bin):
        print(f"dbt not found at {args.dbt_bin!r}", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parent.parent
    token = load_token()
    dbt_dir = repo / args.dbt_dir

    runs: list[RunMetrics] = []
    # Interleaved rather than blocked, so any drift in machine load or catalog
    # size is shared between the arms instead of landing on whichever ran last.
    for index in range(1, args.runs + 1):
        for arm in ("off", "on"):
            print(f"[{arm:>3} {index}/{args.runs}] ", end="", flush=True)
            metrics = one_run(arm=arm, index=index, token=token, dbt_dir=dbt_dir,
                              dbt_bin=args.dbt_bin, pack_root=args.pack_root)
            runs.append(metrics)
            print(f"ttrc={metrics.time_to_root_cause_s:6.2f}s "
                  f"post={metrics.post_detection_s:5.2f}s "
                  f"tools={metrics.mcp_tool_calls} docs={metrics.documents_retrieved} "
                  f"verdict={metrics.diagnostician_verdict}")

    summaries = {arm: ArmSummary.of(arm, [r for r in runs if r.arm == arm])
                 for arm in ("on", "off")}
    comparison = compare(summaries["on"], summaries["off"])

    out = Path(repo / args.out)
    timings = write_timings(out / "timings.json", runs, summaries, comparison)
    for run in runs:
        (out / "raw").mkdir(parents=True, exist_ok=True)
        (out / "raw" / f"{run.arm}-{run.run_index:02d}.json").write_text(
            json.dumps(run.to_dict(), indent=2) + "\n")

    print()
    for arm in ("on", "off"):
        s = summaries[arm]
        print(f"retrieval={arm:<3} n={s.n}  ttrc median {s.ttrc_median_s:6.2f}s "
              f"(min {s.ttrc_min_s:.2f} max {s.ttrc_max_s:.2f})  "
              f"post-detection median {s.post_detection_median_s:.2f}s  "
              f"tools {s.mcp_tool_calls_median:.0f}  tokens {s.tokens_total}  "
              f"${s.usd_cost_total}")
    print()
    print(f"delta ttrc median          : {comparison['delta_ttrc_median_s']:+.4f}s")
    print(f"delta post-detection median: {comparison['delta_post_detection_median_s']:+.4f}s")
    print(f"retrieval could affect root cause: "
          f"{comparison['retrieval_could_affect_root_cause']}")
    print(f"  -> {comparison['interpretation']}")
    print(f"\ntimings: {timings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
