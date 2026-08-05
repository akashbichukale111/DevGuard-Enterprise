#!/usr/bin/env python
"""
run_d5_diagnosis.py — demonstrate diagnosis, and demonstrate refusal.

Runs the read-side collection, then hands the chain to the Diagnostician.

Two scenarios, and the second is the gate:

  --scenario full     DataHub reachable. The chain forms, is sufficient, and the
                      Diagnostician is asked to reason. In this environment that
                      ends in REASONER_UNAVAILABLE, because api.groq.com is
                      blocked — reported as BLOCKED, never as a refusal.

  --scenario refusal  DataHub is NOT reachable (point --gms-url at a closed
                      port). The Watcher still observes a real dbt failure, so
                      RUNTIME evidence is genuine, but no DATAHUB_GRAPH evidence
                      can be collected. The evidence chain cannot form and the
                      Diagnostician refuses, without consulting any model.

Nothing is simulated in either scenario. The refusal scenario points at a port
where nothing is listening; the tool calls really fail. This is also a realistic
production case — "the catalog is down" is not a hypothetical — which is why it
was chosen over contriving an artificial gap in the evidence.

Note on structure: this deliberately does NOT import from
`run_d4_evidence_chain.py`. That script and its proof pack are verified,
approved D4 evidence; refactoring it to share code here would mean re-running it
to prove the refactor, which would overwrite that evidence. The ~50 lines of
overlap are a knowing cost, to be consolidated once both runners are stable.

Usage:
    DATAHUB_TOKEN_FILE=<token> DBT_BIN=<dbt> \\
      python scripts/run_d5_diagnosis.py --scenario refusal
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

from backend.v2.agents.archivist import Archivist
from backend.v2.agents.cartographer import Cartographer
from backend.v2.agents.diagnostician import (
    Diagnostician, GroqReasoner, Verdict,
)
from backend.v2.agents.pathfinder import Pathfinder
from backend.v2.agents.watcher import RuntimeEvidenceProvider, Watcher
from backend.v2.datahub_client import (
    DataHubMCPClient, DataHubUnavailableError, load_token,
)
from backend.v2.evidence import EvidenceBuilder, EvidenceChain
from backend.v2.handoff import AgentHandoff
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import Sentinel

RAW_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
FEATURES = ("urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "devguard.analytics_marts.user_order_features,PROD)")

#: Nothing listens here. Used by --scenario refusal to make the catalog genuinely
#: unreachable rather than pretending it is.
DEAD_GMS_URL = "http://localhost:1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("full", "refusal"), default="full")
    parser.add_argument("--incident-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--pack-root", default="evidence/proof-pack")
    parser.add_argument("--dbt-dir", default="substrate/dbt")
    parser.add_argument("--dbt-bin", default=os.environ.get("DBT_BIN", "dbt"))
    parser.add_argument("--gms-url", default=None)
    args = parser.parse_args()

    incident_id = args.incident_id or f"D5{args.scenario[:3].upper()}"
    run_id = args.run_id or f"d5-{args.scenario}"
    gms_url = args.gms_url or (DEAD_GMS_URL if args.scenario == "refusal"
                               else "http://localhost:8080")

    pack = ProofPack(args.pack_root, run_id)
    builder = EvidenceBuilder(incident_id)
    chain = EvidenceChain(incident_id=incident_id)
    handoffs: list[AgentHandoff] = []
    sentinel = Sentinel()

    if not shutil.which(args.dbt_bin):
        print(f"dbt not found at {args.dbt_bin!r}; set DBT_BIN", file=sys.stderr)
        return 2

    print(f"=== scenario: {args.scenario}   GMS: {gms_url}")

    # -- the runtime half. Real, in both scenarios. --------------------------
    print("[watcher] running dbt against the live substrate ...")
    env = dict(os.environ, DBT_PROFILES_DIR=".")
    failure = RuntimeEvidenceProvider(cwd=args.dbt_dir).observe([args.dbt_bin, "run"],
                                                               env=env)
    items, handoff = Watcher(builder, pack).run(failure)
    chain = chain.add(*items)
    handoffs.append(handoff)
    print(f"[watcher] exit={failure.exit_code} column={failure.missing_column} "
          f"-> {handoff.decision.value}")

    # -- the graph half. Genuinely unavailable in the refusal scenario. ------
    datahub_error: str | None = None
    try:
        with DataHubMCPClient(token=load_token(), gms_url=gms_url) as client:
            caps = client.capabilities
            print(f"[mcp] {len(caps.tools)} tools negotiated")

            archivist = Archivist(client, builder, pack, sentinel)
            knowledge, arch_evidence, arch_handoff = archivist.run(
                failure.missing_column or "schema drift")
            chain = chain.add(*arch_evidence)
            handoffs.append(arch_handoff)
            print(f"[archivist] {arch_handoff.decision.value}")

            cartographer = Cartographer(client, builder, pack, sentinel)
            term = (failure.failing_model or "stg_users").split(".")[-1]
            asset, cart_evidence, cart_handoff = cartographer.run(
                term, prefer_platform="postgres")
            chain = chain.add(*cart_evidence)
            handoffs.append(cart_handoff)
            print(f"[cartographer] {cart_handoff.decision.value}: {asset.urn}")

            pathfinder = Pathfinder(client, builder, pack)
            radius, path_evidence, path_handoff = pathfinder.run(
                RAW_USERS, column=failure.missing_column, terminal_urn=FEATURES,
                max_hops=5)
            chain = chain.add(*path_evidence)
            handoffs.append(path_handoff)
            print(f"[pathfinder] {path_handoff.decision.value}: "
                  f"{len(radius.impacted)} impacted")
    except (DataHubUnavailableError, OSError) as exc:
        # Captured, not swallowed. The chain is now genuinely one-sided, and
        # that is exactly the condition the evidence rule exists to catch.
        datahub_error = f"{type(exc).__name__}: {exc}"
        ref = pack.write("datahub-unavailable.txt",
                         f"GMS URL: {gms_url}\n\n{datahub_error}\n",
                         note="The catalog could not be reached. No DATAHUB_GRAPH "
                              "evidence exists for this run.")
        print(f"[mcp] UNAVAILABLE -> {datahub_error.splitlines()[0][:120]}")
        print(f"[mcp] captured at {ref}")

    # -- step 9: the Diagnostician -------------------------------------------
    reasoner = GroqReasoner() if os.environ.get("GROQ_API_KEY") else None
    diagnostician = Diagnostician(reasoner=reasoner, pack=pack, sentinel=sentinel)
    diagnosis, diag_handoff = asyncio.run(diagnostician.run(chain))
    handoffs.append(diag_handoff)

    print()
    print(f"[diagnostician] verdict   : {diagnosis.verdict.value}")
    print(f"[diagnostician] is_refusal: {diagnosis.is_refusal}")
    print(f"[diagnostician] model     : {diagnosis.model}")
    print(f"[diagnostician] statement : {diagnosis.statement}")

    pack.write("evidence-chain.json", {
        "incident_id": chain.incident_id,
        "digest": chain.digest(),
        "is_sufficient": chain.is_sufficient,
        "missing_for_sufficiency": [s.value for s in chain.missing_for_sufficiency],
        "datahub_error": datahub_error,
        "items": [json.loads(e.model_dump_json()) for e in chain.items],
    }, note="The evidence chain for this scenario.")

    pack.write("diagnosis.json", {
        "verdict": diagnosis.verdict.value,
        "is_refusal": diagnosis.is_refusal,
        "statement": diagnosis.statement,
        "cited_evidence_ids": list(diagnosis.cited_evidence_ids),
        "refusal_reasons": [r.value for r in diagnosis.refusal_reasons],
        "model": diagnosis.model,
        "tokens": diagnosis.tokens,
    }, note="The Diagnostician's typed output.")

    pack.write("handoff-rail.json", [
        json.loads(h.model_dump_json()) | {"duration_ms": round(h.duration_ms, 1)}
        for h in handoffs
    ], note="The envelopes, in order.")

    pack.write_index({
        "scenario": args.scenario,
        "gms_url": gms_url,
        "incident_id": incident_id,
        "chain_is_sufficient": chain.is_sufficient,
        "diagnostician_verdict": diagnosis.verdict.value,
        "diagnostician_is_refusal": diagnosis.is_refusal,
    })

    print()
    print(f"evidence items      : {len(chain.items)}")
    print(f"sources             : {sorted(s.value for s in chain.sources)}")
    print(f"CHAIN IS SUFFICIENT : {chain.is_sufficient}")
    print(f"proof pack          : {pack.root}")

    # Exit code encodes the outcome so `make verify` can assert on it.
    if args.scenario == "refusal":
        return 0 if diagnosis.is_refusal else 1
    return 0 if diagnosis.verdict is not Verdict.INSUFFICIENT_EVIDENCE else 1


if __name__ == "__main__":
    raise SystemExit(main())
