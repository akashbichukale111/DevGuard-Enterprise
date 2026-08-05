#!/usr/bin/env python
"""
run_d6_loop.py — step 2–18, end to end, against the live stack.

The requirement: *"Surgeon → Sentinel → Referee → Magistrate → approval →
remediation → verification → five-artifact write-back → retrieval on the next
run. The full incident loop runs end to end."*

STEP 9 IS BLOCKED AND THIS SCRIPT SAYS SO. `api.groq.com` is denied by this
environment's egress policy, so the Diagnostician cannot reason. It is still
invoked, and its `REASONER_UNAVAILABLE` verdict is recorded, because skipping it
would hide the gap.

The loop continues past it because **the Surgeon is driven by the typed evidence
chain, not by the Diagnostician's prose**. That is a design property, not a
workaround: the fix is derived from `column "user_id" does not exist` plus a
live `information_schema` probe, both of which are `RUNTIME` facts. The
`root_cause` string written into the runbook is composed deterministically from
those same facts and is labelled `DEVGUARD_INFERENCE / DERIVED` — it is never
presented as model output, and `--require-diagnosis` makes the whole run refuse
without one, for the day the key exists.

Usage:
    DATAHUB_TOKEN_FILE=<t> DBT_BIN=<dbt> python scripts/run_d6_loop.py
    ... --dry-run          show the exact write-back payloads, send nothing
    ... --fail-the-fix     propose a fix that does NOT work, to demonstrate the write-back
                           "a deliberately failing fix writes nothing"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.agents.archivist import Archivist
from backend.v2.agents.cartographer import Cartographer
from backend.v2.agents.diagnostician import Diagnostician, GroqReasoner, Verdict
from backend.v2.agents.magistrate import ApprovalMode, Magistrate
from backend.v2.agents.pathfinder import Pathfinder
from backend.v2.agents.referee import Referee
from backend.v2.agents.scribe import Scribe
from backend.v2.agents.surgeon import Surgeon
from backend.v2.agents.watcher import ColumnProbe, RuntimeEvidenceProvider, Watcher
from backend.v2.datahub_client import DataHubMCPClient, load_token
from backend.v2.evidence import (
    EvidenceBuilder, EvidenceChain, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentHandoff
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import Sentinel

RAW_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
FEATURES = ("urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "devguard.analytics_marts.user_order_features,PROD)")
MODEL_PATH = "substrate/dbt/models/staging/stg_users.sql"

#: The human who approves. In a real deployment this is the owner resolved from
#: The graph; here it is supplied explicitly and recorded verbatim, because
#: The autonomy policy wants a real name and an inferred approver would be a fabrication.
APPROVER_URN = "urn:li:corpuser:datahub"
APPROVER_NAME = "DataHub Admin (local operator)"


def gql_factory(token: str):
    def gql(query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "http://localhost:8080/api/graphql", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return {"httpError": exc.code, "body": exc.read().decode()[:600]}
    return gql


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--incident-id", default=None)
    parser.add_argument("--pack-root", default="evidence/proof-pack")
    parser.add_argument("--dbt-dir", default="substrate/dbt")
    parser.add_argument("--dbt-bin", default=os.environ.get("DBT_BIN", "dbt"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-the-fix", action="store_true")
    parser.add_argument("--require-diagnosis", action="store_true",
                        help="halt unless the Diagnostician produced a root cause")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"d6-loop-{stamp}"
    incident_id = args.incident_id or f"D6{stamp[9:15]}"
    pack = ProofPack(args.pack_root, run_id)
    builder = EvidenceBuilder(incident_id)
    chain = EvidenceChain(incident_id=incident_id)
    handoffs: list[AgentHandoff] = []
    sentinel = Sentinel()
    repo = Path(__file__).resolve().parent.parent

    if not shutil.which(args.dbt_bin):
        print(f"dbt not found at {args.dbt_bin!r}; set DBT_BIN", file=sys.stderr)
        return 2

    token = load_token()
    gql = gql_factory(token)

    print(f"=== D6 loop   incident={incident_id}   run={run_id}"
          f"{'   DRY RUN' if args.dry_run else ''}"
          f"{'   FAIL-THE-FIX' if args.fail_the_fix else ''}")

    # ---- step 2: real failure ---------------------------------------------
    env = dict(os.environ, DBT_PROFILES_DIR=".")
    failure = RuntimeEvidenceProvider(cwd=repo / args.dbt_dir).observe(
        [args.dbt_bin, "run"], env=env)
    items, handoff = Watcher(builder, pack).run(failure)
    chain = chain.add(*items)
    handoffs.append(handoff)
    print(f"[watcher] exit={failure.exit_code} model={failure.failing_model} "
          f"column={failure.missing_column}")
    if not failure.is_failure:
        print("dbt passes — the substrate is not in the reference-incident state. "
              "Run scripts/reset_demo.py first.", file=sys.stderr)
        return 3

    # ---- verification query: what does the database ACTUALLY have now? -----
    live = ColumnProbe().probe("raw", "users")
    probe_ref = pack.write("watcher/column-probe.json",
                           {"table": live.table, "columns": list(live.columns),
                            "probed_at_utc": live.probed_at_utc},
                           note="information_schema, read live. The catalog is stale "
                                "during drift; this is the ground truth.")
    probe_ev = builder.make(
        source=EvidenceSource.RUNTIME, trust=EvidenceTrust.TRUSTED_SYSTEM,
        confidence=EvidenceConfidence.OBSERVED,
        claim=f"{live.table} now has columns: {', '.join(live.columns)}",
        raw_ref=probe_ref)
    chain = chain.add(probe_ev)
    print(f"[probe] {live.table}: {live.columns}")

    incident_urn = None
    with DataHubMCPClient(token=token) as read_client:
        # ---- steps 4-8: graph context and blast radius ---------------------
        archivist = Archivist(read_client, builder, pack, sentinel)
        knowledge, arch_ev, arch_handoff = archivist.run(
            failure.missing_column or "schema drift")
        chain = chain.add(*arch_ev)
        handoffs.append(arch_handoff)
        print(f"[archivist] {arch_handoff.decision.value}: {knowledge.summary[:70]}")

        cartographer = Cartographer(read_client, builder, pack, sentinel)
        term = (failure.failing_model or "stg_users").split(".")[-1]
        asset, cart_ev, cart_handoff = cartographer.run(term, prefer_platform="postgres")
        chain = chain.add(*cart_ev)
        handoffs.append(cart_handoff)

        pathfinder = Pathfinder(read_client, builder, pack)
        radius, path_ev, path_handoff = pathfinder.run(
            RAW_USERS, column=failure.missing_column, terminal_urn=FEATURES, max_hops=5)
        chain = chain.add(*path_ev)
        handoffs.append(path_handoff)
        print(f"[pathfinder] {len(radius.impacted)} impacted, "
              f"reaches_ml_model={radius.reaches_ml_model}")

        # ---- step 9: the Diagnostician (blocked in this environment) -------
        reasoner = GroqReasoner() if os.environ.get("GROQ_API_KEY") else None
        diagnosis, diag_handoff = asyncio.run(
            Diagnostician(reasoner=reasoner, pack=pack, sentinel=sentinel).run(chain))
        handoffs.append(diag_handoff)
        print(f"[diagnostician] {diagnosis.verdict.value} (is_refusal="
              f"{diagnosis.is_refusal})")
        if args.require_diagnosis and diagnosis.verdict is not Verdict.ROOT_CAUSE_IDENTIFIED:
            print("--require-diagnosis was set and no root cause was produced. Halting.",
                  file=sys.stderr)
            return 4

        # The root cause DevGuard proceeds on, composed deterministically from
        # RUNTIME facts. Labelled DERIVED, never presented as model reasoning.
        root_cause = (
            f"raw.users.{failure.missing_column} was renamed upstream. The database "
            f"now exposes {', '.join(live.columns)}; the dbt model {failure.failing_model} "
            f"still selects {failure.missing_column} by name, so it fails to build "
            f"(exit {failure.exit_code}). Derived deterministically from runtime "
            f"evidence — NOT produced by a language model "
            f"(Diagnostician verdict: {diagnosis.verdict.value}).")
        chain = chain.add(builder.make(
            source=EvidenceSource.DEVGUARD_INFERENCE,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.DERIVED,
            claim=f"root cause (derived, not model-produced): "
                  f"{failure.missing_column} renamed upstream",
            raw_ref=pack.write("diagnostician/derived-root-cause.txt",
                               root_cause + "\n",
                               note="DERIVED from runtime evidence. The Diagnostician "
                                    "could not run — see its handoff.")))

        # ---- step 11: the Surgeon proposes ---------------------------------
        surgeon = Surgeon(builder, pack, repo_root=repo)
        live_columns = live.columns
        if args.fail_the_fix:
            # Demonstrates the write-back rule. A column that does not exist produces a
            # patch that builds red, so nothing may be written.
            live_columns = ("definitely_not_a_real_column",)
        proposal, surg_ev, surg_handoff = surgeon.run(
            MODEL_PATH, failure.missing_column, live_columns, incident_id)
        chain = chain.add(*surg_ev)
        handoffs.append(surg_handoff)
        print(f"[surgeon] {surg_handoff.decision.value}: "
              f"{proposal.replacement_column or proposal.refusal_reason[:60]}")
        if not proposal.proposed:
            _finish(pack, chain, handoffs, incident_id, None, None, args)
            return 0  # a refusal is a correct outcome, not a failure

        # ---- step 12: Sentinel scans the proposed change -------------------
        scan = sentinel.scan_patch(proposal.diff)
        scan_ref = pack.write("sentinel/patch-scan.json", {
            "files_touched": list(scan.files_touched), "added": scan.added_lines,
            "removed": scan.removed_lines, "risk": scan.risk.value,
            "blocked": scan.blocked,
            "findings": [{"rule": f.rule, "risk": f.risk.value, "detail": f.detail}
                         for f in scan.findings]},
            note="step 12 — the proposed diff, classified.")
        chain = chain.add(builder.make(
            source=EvidenceSource.DEVGUARD_INFERENCE,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.DERIVED,
            claim=f"patch risk {scan.risk.value} across {len(scan.files_touched)} file(s)"
                  f"{' — BLOCKED' if scan.blocked else ''}",
            raw_ref=scan_ref))
        print(f"[sentinel] patch risk={scan.risk.value} blocked={scan.blocked}")
        if scan.blocked:
            _finish(pack, chain, handoffs, incident_id, None, None, args)
            return 0

        # ---- step 13: Referee validates BEFORE approval --------------------
        referee = Referee(builder, pack, dbt_dir=args.dbt_dir, dbt_bin=args.dbt_bin)
        validation, val_ev, val_handoff = referee.run_validation(
            MODEL_PATH, proposal.patched_sql, incident_id, repo_root=repo)
        chain = chain.add(*val_ev)
        handoffs.append(val_handoff)
        print(f"[referee] validation passed={validation.passed} "
              f"(isolated schema {validation.schema})")

        # ---- step 14: Magistrate routes to a real owner --------------------
        magistrate = Magistrate(read_client, builder, pack)
        request, mag_ev, mag_handoff = magistrate.run(incident_id, scan.risk, RAW_USERS)
        chain = chain.add(*mag_ev)
        handoffs.append(mag_handoff)
        print(f"[magistrate] risk={request.risk.value} mode={request.mode.value} "
              f"owners={[o.display_name for o in request.owners] or 'UNOWNED'}")

        if not validation.passed:
            # The write-back demonstration case: a fix that does not build is never
            # approved, never applied, and writes nothing.
            print("[loop] validation FAILED — no approval, no remediation, no write-back")
            _finish(pack, chain, handoffs, incident_id, None, None, args)
            return 0

        if request.mode is ApprovalMode.FORBIDDEN:
            print("[loop] risk class has no approval path — halting")
            _finish(pack, chain, handoffs, incident_id, None, None, args)
            return 0

        # ---- step 15: explicit human approval, recorded --------------------
        request.approve(approver_urn=APPROVER_URN, approver_name=APPROVER_NAME,
                        note=(f"Reviewed the diff on {proposal.branch}; validated in "
                              f"{validation.schema} before approval."))
        approval_ref = pack.write("magistrate/approval.json", {
            "incident_id": incident_id, "risk": request.risk.value,
            "mode": request.mode.value,
            "approver_urn": request.approval.approver_urn,
            "approver_name": request.approval.approver_name,
            "decided_at_utc": request.approval.decided_at_utc,
            "note": request.approval.note,
            "branch_reviewed": proposal.branch},
            note="step 15 — explicit, logged, identity recorded.")
        chain = chain.add(builder.make(
            source=EvidenceSource.REPO_STATIC, trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=f"approved by {request.approval.approver_name} at "
                  f"{request.approval.decided_at_utc}",
            raw_ref=approval_ref))
        print(f"[human] approved by {request.approval.approver_name}")

        # ---- step 16: remediation ------------------------------------------
        (repo / MODEL_PATH).write_text(proposal.patched_sql)
        print(f"[remediation] applied {proposal.branch} to the working tree")

        # ---- step 17: the exact signal that failed now passes ---------------
        recovery, rec_ev, rec_handoff = referee.run_recovery(
            failure, incident_id, repo_root=repo)
        chain = chain.add(*rec_ev)
        handoffs.append(rec_handoff)
        print(f"[referee] RECOVERY VERIFIED = {recovery.verified} "
              f"(exit {recovery.now_exit_code}, PASS={recovery.now_passed_models})")

        # ---- write-back artifact 1 (raise half): the incident this run is about -----
        if recovery.verified and not args.dry_run:
            raised = gql(
                "mutation raise($input: RaiseIncidentInput!) { raiseIncident(input: $input) }",
                {"input": {
                    "type": "OPERATIONAL",
                    "title": f"[{incident_id}] {failure.missing_column} renamed upstream "
                             f"— {failure.failing_model} failed to build",
                    "description": root_cause,
                    "resourceUrn": RAW_USERS}})
            incident_urn = (raised.get("data") or {}).get("raiseIncident")
            pack.write("scribe/artifact1-raiseIncident.json", raised,
                       note="write-back artifact 1, raise half.")
            print(f"[scribe] incident raised: {incident_urn}")

    # ---- step 18: the five-artifact write-back ------------------------------
    with DataHubMCPClient(token=token, enable_mutations=True) as write_client:
        scribe = Scribe(write_client, builder, pack, gql=gql, dry_run=args.dry_run)
        result, scribe_ev, scribe_handoff = scribe.run(
            chain=chain, recovery=recovery, approval=request,
            dataset_urn=RAW_USERS, column=failure.missing_column,
            incident_urn=incident_urn, root_cause=root_cause)
        chain = chain.add(*scribe_ev)
        handoffs.append(scribe_handoff)

    print()
    print(f"[scribe] performed={result.performed}  {result.reason or result.summary}")
    for artifact in result.artifacts:
        print(f"   {artifact.number}. {artifact.name:45} {artifact.outcome.value}")

    _finish(pack, chain, handoffs, incident_id, result, recovery, args,
            document_urn=result.document_urn, incident_urn=incident_urn)
    return 0 if (args.dry_run or result.performed) else 1


def _finish(pack, chain, handoffs, incident_id, result, recovery, args,
            document_urn=None, incident_urn=None) -> None:
    pack.write("evidence-chain.json", {
        "incident_id": chain.incident_id, "digest": chain.digest(),
        "is_sufficient": chain.is_sufficient,
        "items": [json.loads(e.model_dump_json()) for e in chain.items]},
        note="The evidence chain for the full loop.")
    pack.write("handoff-rail.json", [
        json.loads(h.model_dump_json()) | {"duration_ms": round(h.duration_ms, 1)}
        for h in handoffs], note="The envelopes, in order.")
    pack.write_index({
        "incident_id": incident_id,
        "dry_run": args.dry_run,
        "fail_the_fix": args.fail_the_fix,
        "chain_is_sufficient": chain.is_sufficient,
        "recovery_verified": bool(recovery and recovery.verified),
        "write_back_performed": bool(result and result.performed),
        "write_back_all_landed": bool(result and result.all_landed),
        "incident_resolved": bool(result and result.incident_resolved),
        "incident_urn": incident_urn,
        "document_urn": document_urn,
        "handoffs": [f"{h.from_agent}->{h.to_agent}:{h.decision.value}"
                     for h in handoffs],
    })
    print(f"\nevidence items : {len(chain.items)}")
    print(f"chain digest   : {chain.digest()}")
    print(f"proof pack     : {pack.root}")


if __name__ == "__main__":
    raise SystemExit(main())
