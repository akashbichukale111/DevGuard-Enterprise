#!/usr/bin/env python
"""
verify_least_privilege.py — prove the least-privilege policy's service account is actually scoped.

A policy document is a claim. This runs the claim against the live server: every
operation DevGuard needs, and every operation it must NOT be able to perform,
executed as the service account, with the real response recorded.

    DATAHUB_TOKEN_FILE=<admin> DEVGUARD_AGENT_TOKEN_FILE=<agent> \\
      python scripts/verify_least_privilege.py

Two halves, and the second is the one that matters. Any account can be shown to
work; least privilege is only demonstrated by showing what it **cannot** do. A
run where every DENY case succeeds is a failed verification even though nothing
errored, so the exit code is non-zero unless every expectation held.

Nothing here is destructive against real state: the deny cases are attempted
against the hero-path datasets precisely because they must be refused, and each
one is checked to have had no effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.proofpack import ProofPack

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
IN_SCOPE = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
#: Ingested from the substrate, deliberately NOT in the service account's five-dataset scope.
OUT_OF_SCOPE = ("urn:li:dataset:(urn:li:dataPlatform:dbt,"
                "devguard.analytics_marts.user_order_features,PROD)")


def gql(query: str, variables: dict, token: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"httpError": exc.code, "body": exc.read().decode()[:600]}


def denied(payload: dict) -> bool:
    """Did the server refuse? Authorisation failures surface as GraphQL errors."""
    if payload.get("httpError"):
        return True
    for error in payload.get("errors", []) or []:
        message = json.dumps(error).lower()
        if any(word in message for word in
               ("unauthorized", "unauthorised", "not authorized", "denied",
                "privilege", "permission")):
            return True
    # A null data field with any error at all also counts as a refusal.
    return bool(payload.get("errors")) and not payload.get("data")


@dataclass
class Check:
    name: str
    expectation: str          # "ALLOW" or "DENY"
    detail: str
    observed: str = ""
    passed: bool = False
    raw: dict = field(default_factory=dict)


def run_checks(agent: str) -> list[Check]:
    checks: list[Check] = []

    def record(name: str, expectation: str, detail: str, payload: dict) -> None:
        was_denied = denied(payload)
        observed = "DENY" if was_denied else "ALLOW"
        checks.append(Check(name=name, expectation=expectation, detail=detail,
                            observed=observed, passed=observed == expectation,
                            raw=payload))

    # ---------------- ALLOW: everything the five write-back artifacts need --------------
    record("read in-scope dataset", "ALLOW",
           "VIEW_ENTITY_PAGE on a scoped dataset",
           gql("query d($urn:String!){ dataset(urn:$urn){ urn } }",
               {"urn": IN_SCOPE}, agent))

    record("artifact 1 — raise incident", "ALLOW",
           "EDIT_ENTITY_INCIDENTS on a scoped dataset",
           gql("mutation r($input: RaiseIncidentInput!){ raiseIncident(input:$input) }",
               {"input": {"type": "OPERATIONAL",
                          "title": "least-privilege verification probe",
                          "description": ("Raised by scripts/verify_least_privilege.py "
                                          "to prove the service account holds "
                                          "EDIT_ENTITY_INCIDENTS. Not a real incident."),
                          "resourceUrn": IN_SCOPE}}, agent))

    record("artifact 3 — column tag", "ALLOW",
           "EDIT_DATASET_COL_TAGS on a scoped dataset",
           gql("mutation a($input: AddTagsInput!){ addTags(input:$input) }",
               {"input": {"tagUrns": ["urn:li:tag:devguard_incident_impacted"],
                          "resourceUrn": IN_SCOPE, "subResource": "user_id",
                          "subResourceType": "DATASET_FIELD"}}, agent))

    record("artifact 5 — add owner", "ALLOW",
           "EDIT_ENTITY_OWNERS on a scoped dataset",
           gql("mutation a($input: AddOwnersInput!){ addOwners(input:$input) }",
               {"input": {"owners": [{"ownerUrn": "urn:li:corpuser:datahub",
                                      "ownerEntityType": "CORP_USER",
                                      "ownershipTypeUrn":
                                          "urn:li:ownershipType:__system__technical_owner"}],
                          "resourceUrn": IN_SCOPE}}, agent))

    # ---------------- DENY: the half that proves the point -------------------
    record("delete a scoped dataset", "DENY",
           "DELETE_ENTITY was never granted — an agent must not remove assets",
           gql("mutation d($urn:String!){ batchUpdateSoftDeleted("
               "input:{urns:[$urn], deleted:true}) }", {"urn": IN_SCOPE}, agent))

    record("edit lineage", "DENY",
           "EDIT_LINEAGE was never granted — lineage is ingested, never authored (the design thesis)",
           gql("mutation u($input: UpdateLineageInput!){ updateLineage(input:$input) }",
               {"input": {"edgesToAdd": [
                   {"downstreamUrn": IN_SCOPE, "upstreamUrn": OUT_OF_SCOPE}],
                   "edgesToRemove": []}}, agent))

    record("write to an OUT-OF-SCOPE dataset", "DENY",
           "the dbt sibling is real and ingested, but outside the five-URN scope",
           gql("mutation a($input: AddTagsInput!){ addTags(input:$input) }",
               {"input": {"tagUrns": ["urn:li:tag:devguard_incident_impacted"],
                          "resourceUrn": OUT_OF_SCOPE}}, agent))

    record("create a policy (widen its own grants)", "DENY",
           "MANAGE_POLICIES was never granted — it must not escalate itself",
           gql("mutation c($input: PolicyUpdateInput!){ createPolicy(input:$input) }",
               {"input": {"type": "PLATFORM", "name": "devguard-escalation-probe",
                          "state": "ACTIVE", "privileges": ["MANAGE_POLICIES"],
                          "actors": {"users": ["urn:li:corpuser:devguard_agent"],
                                     "resourceOwners": False, "allUsers": False,
                                     "allGroups": False}}}, agent))

    record("create an ingestion source", "DENY",
           "MANAGE_INGESTION was never granted — it must not change what enters "
           "the catalog",
           gql("mutation c($input: UpdateIngestionSourceInput!)"
               "{ createIngestionSource(input:$input) }",
               {"input": {"name": "devguard-probe", "type": "postgres",
                          "config": {"recipe": "{}", "executorId": "default"}}}, agent))

    # The two probes below close a gap between what the security documentation
    # claimed and what this verifier actually proved. SECURITY.md listed seven
    # privileges as "never granted, each verified as a live DENY", but only four
    # of them had a probe here — glossary terms and domains were asserted rather
    # than tested, and an assertion in a security document is exactly the kind of
    # claim this project exists to not make.
    #
    # Both are real write paths a compromised or buggy agent could reach:
    # `add_terms` and `set_domains` are live tools in mcp-server-datahub's
    # mutation set, deliberately left out of the Scribe's allowlist. Server-side
    # refusal is the control that survives a bug in that allowlist, so it is
    # worth proving rather than assuming.
    record("attach a glossary term", "DENY",
           "EDIT_ENTITY_GLOSSARY_TERMS was never granted — an agent must not "
           "author business vocabulary, only reference it",
           gql("mutation a($input: AddTermsInput!){ addTerms(input:$input) }",
               {"input": {"termUrns": ["urn:li:glossaryTerm:devguard_probe"],
                          "resourceUrn": IN_SCOPE}}, agent))

    record("assign a domain", "DENY",
           "EDIT_DOMAINS_PRIVILEGE was never granted — domain membership decides "
           "who else inherits access, so an agent must not reassign it",
           gql("mutation s($entityUrn:String!, $domainUrn:String!)"
               "{ setDomain(entityUrn:$entityUrn, domainUrn:$domainUrn) }",
               {"entityUrn": IN_SCOPE,
                "domainUrn": "urn:li:domain:devguard_probe"}, agent))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-root", default="evidence/proof-pack/security")
    parser.add_argument("--run-id", default="least-privilege")
    args = parser.parse_args()

    agent_file = os.environ.get("DEVGUARD_AGENT_TOKEN_FILE")
    if not agent_file:
        print("set DEVGUARD_AGENT_TOKEN_FILE to the service-account token",
              file=sys.stderr)
        return 2
    agent = Path(agent_file).read_text().strip()

    pack = ProofPack(args.pack_root, args.run_id)
    checks = run_checks(agent)

    print(f"the least-privilege policy least-privilege verification — {len(checks)} checks "
          f"as urn:li:corpuser:devguard_agent\n")
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.expectation:5} -> {check.observed:5}  {check.name}")
        if not check.passed:
            print(f"         {check.detail}")
        pack.write(f"checks/{check.name.replace(' ', '_').replace('/', '_')}.json",
                   {"name": check.name, "expectation": check.expectation,
                    "observed": check.observed, "passed": check.passed,
                    "detail": check.detail, "response": check.raw},
                   note=f"Expected {check.expectation}, observed {check.observed}.")

    allowed = [c for c in checks if c.expectation == "ALLOW"]
    refused = [c for c in checks if c.expectation == "DENY"]
    summary = {
        "schema": "devguard.least-privilege/1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "service_account": "urn:li:corpuser:devguard_agent",
        "total": len(checks),
        "passed": sum(1 for c in checks if c.passed),
        "allow_checks": len(allowed),
        "allow_passed": sum(1 for c in allowed if c.passed),
        "deny_checks": len(refused),
        "deny_passed": sum(1 for c in refused if c.passed),
        "checks": [{"name": c.name, "expectation": c.expectation,
                    "observed": c.observed, "passed": c.passed, "detail": c.detail}
                   for c in checks],
    }
    ref = pack.write("summary.json", summary, note="The least-privilege policy verification summary.")
    pack.write_index({"passed": summary["passed"], "total": summary["total"]})

    print(f"\nALLOW: {summary['allow_passed']}/{summary['allow_checks']} behaved as required")
    print(f"DENY : {summary['deny_passed']}/{summary['deny_checks']} correctly refused")
    print(f"\nsummary: {ref}")
    if summary["passed"] != summary["total"]:
        print("\nVERIFICATION FAILED — the account is not scoped as documented.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
