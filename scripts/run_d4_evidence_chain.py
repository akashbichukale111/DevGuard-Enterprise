#!/usr/bin/env python
"""
run_d4_evidence_chain.py — form a real evidence chain against the live stack.

Runs the read side of the incident loop against the live stack and writes everything
it saw into a proof pack:

    step 2   REAL failure           Watcher       -> RUNTIME evidence
    step 4   capability negotiation Archivist     -> the negotiated tool set
    step 5   graph context          Cartographer  -> URNs + catalog schema
    step 6   blast radius           Pathfinder    -> lineage to the mlModel
    step 7   untrusted-text screen  Sentinel      -> every catalog string fenced
    step 8   prior knowledge        Archivist     -> a hit, or an explicit miss

It stops at step 8. Step 9 (root cause) is the Diagnostician and is D5 work —
this script deliberately does not diagnose anything, it only establishes whether
The evidence chain can form.

Nothing here simulates. The Watcher runs a real `dbt run` against the real
substrate, which is still broken from D3, and reports whatever happens. If
someone repairs the substrate before running this, it will honestly say the
command passed and refuse to build a chain.

Usage:
    DATAHUB_TOKEN_FILE=/path/to/token python scripts/run_d4_evidence_chain.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.agents.archivist import Archivist
from backend.v2.agents.cartographer import Cartographer
from backend.v2.agents.pathfinder import Pathfinder
from backend.v2.agents.watcher import RuntimeEvidenceProvider, Watcher
from backend.v2.datahub_client import DataHubMCPClient, load_token
from backend.v2.evidence import EvidenceBuilder, EvidenceChain
from backend.v2.handoff import AgentHandoff
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import Sentinel

# The hero-path URNs, verified live in D2/D3.
RAW_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
FEATURES = ("urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "devguard.analytics_marts.user_order_features,PROD)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident-id", default="D4")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--pack-root", default="evidence/proof-pack")
    parser.add_argument("--dbt-dir", default="substrate/dbt")
    parser.add_argument("--dbt-bin", default=os.environ.get("DBT_BIN", "dbt"))
    args = parser.parse_args()

    run_id = args.run_id or f"d4-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    pack = ProofPack(args.pack_root, run_id)
    builder = EvidenceBuilder(args.incident_id)
    chain = EvidenceChain(incident_id=args.incident_id)
    handoffs: list[AgentHandoff] = []
    sentinel = Sentinel()

    if not shutil.which(args.dbt_bin):
        print(f"dbt not found at {args.dbt_bin!r}; set DBT_BIN", file=sys.stderr)
        return 2

    # -- step 2: a real failure, observed ------------------------------------
    print("[watcher] running dbt against the live substrate ...")
    env = dict(os.environ, DBT_PROFILES_DIR=".")
    failure = RuntimeEvidenceProvider(cwd=args.dbt_dir).observe([args.dbt_bin, "run"], env=env)
    items, handoff = Watcher(builder, pack).run(failure)
    chain = chain.add(*items)
    handoffs.append(handoff)
    print(f"[watcher] exit={failure.exit_code} model={failure.failing_model} "
          f"column={failure.missing_column} decision={handoff.decision.value}")

    # -- the DataHub half ----------------------------------------------------
    with DataHubMCPClient(token=load_token()) as client:
        caps = client.capabilities
        print(f"[mcp] {len(caps.tools)} tools negotiated "
              f"(server {caps.server_name} {caps.server_version})")

        # step 4 + step 8
        archivist = Archivist(client, builder, pack, sentinel)
        query = failure.missing_column or failure.failing_model or "schema drift"
        knowledge, arch_evidence, arch_handoff = archivist.run(query)
        chain = chain.add(*arch_evidence)
        handoffs.append(arch_handoff)
        print(f"[archivist] {arch_handoff.decision.value}: {knowledge.summary}")

        # step 5 — resolve the failing model to a URN, read catalog schema
        cartographer = Cartographer(client, builder, pack, sentinel)
        term = (failure.failing_model or "stg_users").split(".")[-1]
        asset, cart_evidence, cart_handoff = cartographer.run(term, prefer_platform="postgres")
        chain = chain.add(*cart_evidence)
        handoffs.append(cart_handoff)
        print(f"[cartographer] {cart_handoff.decision.value}: {asset.urn} "
              f"({len(asset.fields)} columns)")

        # step 6 — blast radius from the ROOT of the drift, not the failing model.
        # The rename happened on raw.users; stg_users is a casualty. Rooting the
        # radius at the casualty would understate the impact and would never
        # reach the ML model.
        pathfinder = Pathfinder(client, builder, pack)
        radius, path_evidence, path_handoff = pathfinder.run(
            RAW_USERS, column=failure.missing_column, terminal_urn=FEATURES, max_hops=5)
        chain = chain.add(*path_evidence)
        handoffs.append(path_handoff)
        print(f"[pathfinder] {path_handoff.decision.value}: {len(radius.impacted)} impacted, "
              f"reaches_ml_model={radius.reaches_ml_model}")

    # -- the gate ------------------------------------------------------------
    chain_ref = pack.write("evidence-chain.json", {
        "incident_id": chain.incident_id,
        "digest": chain.digest(),
        "is_sufficient": chain.is_sufficient,
        "missing_for_sufficiency": [s.value for s in chain.missing_for_sufficiency],
        "items": [json.loads(e.model_dump_json()) for e in chain.items],
    }, note="The evidence chain. is_sufficient is the D4 gate.")

    pack.write("handoff-rail.json", [
        json.loads(h.model_dump_json()) | {"duration_ms": round(h.duration_ms, 1)}
        for h in handoffs
    ], note="The AgentHandoff envelopes, in order. The UI rail renders these.")

    pack.write_index({
        "incident_id": args.incident_id,
        "evidence_chain_digest": chain.digest(),
        "chain_is_sufficient": chain.is_sufficient,
        "handoffs": [f"{h.from_agent}->{h.to_agent}:{h.decision.value}" for h in handoffs],
    })

    print()
    print(f"evidence items      : {len(chain.items)}")
    print(f"sources             : {sorted(s.value for s in chain.sources)}")
    print(f"untrusted items     : {len(chain.untrusted_items)}")
    print(f"chain digest        : {chain.digest()}")
    print(f"CHAIN IS SUFFICIENT : {chain.is_sufficient}")
    if not chain.is_sufficient:
        print(f"missing             : {[s.value for s in chain.missing_for_sufficiency]}")
    print(f"proof pack          : {chain_ref.rsplit('/', 1)[0]}")
    return 0 if chain.is_sufficient else 1


if __name__ == "__main__":
    raise SystemExit(main())
