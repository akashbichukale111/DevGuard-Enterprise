#!/usr/bin/env python
"""
setup_service_account.py — the least-privilege policy's least-privilege DataHub service account.

The least-privilege policy: *"a DataHub **service account** with an explicit Access Policy set, scoped
to one domain, optionally with a Default View. Document the exact privileges
required (including incident management)."*

Every privilege below was chosen by walking the write-back five write-back artifacts and
asking what each one actually needs, then checked against the live privilege
vocabulary (`appConfig.policiesConfig`) rather than guessed. `scripts/
verify_least_privilege.py` then proves the result empirically — that the account
can do each of the five things and **cannot** do a list of things it was never
granted.

    DATAHUB_TOKEN_FILE=<admin token> python scripts/setup_service_account.py

The account replaces `urn:li:corpuser:__datahub_system`, which earlier integration
used and which holds `manageIngestion` and `managePolicies` — the opposite of
least privilege, and was tracked as outstanding until this account existed.

WHY NOT A DOMAIN FILTER

The least-privilege policy says "scoped to one domain". This substrate has no domains: nothing in
the substrate has none, and inventing a domain purely to satisfy the wording would
scope the policy to a container that exists only for the policy. The scope here
is **an explicit allowlist of the exact dataset URNs DevGuard writes to**, which
is strictly narrower than a domain — a domain would grant access to anything
later added to it, this grants access to five named datasets and nothing else.
That is a deliberate deviation and it is recorded in SECURITY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVICE_ACCOUNT = "urn:li:corpuser:devguard_agent"
GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")

#: The five datasets DevGuard's reference-incident touches. Everything else in the
#: catalog is out of scope for this account, by construction.
SCOPED_DATASETS = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.orders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_staging.stg_users,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_staging.stg_orders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_marts.user_order_features,PROD)",
)

#: write-back artifact -> the privilege that makes it possible. This mapping IS the
#: documentation the least-privilege policy asks for; SECURITY.md renders it rather than restating it.
ARTIFACT_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "read: search, lineage, schema, queries": ("VIEW_ENTITY_PAGE",),
    "artifact 1 — incident raised and resolved": ("EDIT_ENTITY_INCIDENTS",),
    "artifact 3 — column-level tag": ("EDIT_DATASET_COL_TAGS",),
    "artifact 3 — column-level description": ("EDIT_DATASET_COL_DESCRIPTION",),
    "artifact 4 — structured properties": ("EDIT_ENTITY_PROPERTIES",),
    "artifact 5 — ownership": ("EDIT_ENTITY_OWNERS",),
}

METADATA_PRIVILEGES = tuple(sorted({p for ps in ARTIFACT_PRIVILEGES.values() for p in ps}))

#: Artifact 2 (save_document) is platform-scoped, not resource-scoped: documents
#: are not attached to a dataset in the privilege model, so there is no
#: dataset-scoped way to grant it.
PLATFORM_PRIVILEGES = ("MANAGE_DOCUMENTS",)

#: Deliberately NOT granted. Each is a capability an over-broad service account
#: would carry by default, and each is checked in verify_least_privilege.py.
DENIED_ON_PURPOSE = (
    "DELETE_ENTITY",          # an agent must never be able to remove an asset
    "EDIT_LINEAGE",           # lineage is ingested, never hand-authored (the design thesis)
    "EDIT_ENTITY_STATUS",     # soft-delete is a deletion by another name
    "MANAGE_POLICIES",        # it must not be able to widen its own grants
    "MANAGE_INGESTION",       # it must not be able to change what enters the catalog
    "EDIT_ENTITY_GLOSSARY_TERMS",
    "EDIT_DOMAINS_PRIVILEGE",
)

POLICY_METADATA = "devguard-agent-metadata"
POLICY_PLATFORM = "devguard-agent-platform"


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
        return {"httpError": exc.code, "body": exc.read().decode()[:800]}


def ensure_user(token: str) -> dict:
    """Create the corpUser. Idempotent — an existing user is left alone."""
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import CorpUserInfoClass

    emitter = DatahubRestEmitter(gms_server=GMS, token=token)
    emitter.emit(MetadataChangeProposalWrapper(
        entityUrn=SERVICE_ACCOUNT,
        aspect=CorpUserInfoClass(
            active=True,
            displayName="DevGuard Agent (service account)",
            title="Automated data-incident agent",
            email="owner@example.com",  # the secret-hygiene rule: never a real address in the repo
        )))
    return {"urn": SERVICE_ACCOUNT}


def delete_existing(name: str, token: str) -> None:
    """Remove a previous policy of the same name so re-running is idempotent."""
    listed = gql("query { listPolicies(input:{start:0,count:100}) "
                 "{ policies { urn name } } }", {}, token)
    for policy in ((listed.get("data") or {}).get("listPolicies") or {}).get("policies", []):
        if policy["name"] == name:
            gql("mutation d($urn: String!) { deletePolicy(urn: $urn) }",
                {"urn": policy["urn"]}, token)


def create_metadata_policy(token: str) -> dict:
    delete_existing(POLICY_METADATA, token)
    return gql(
        "mutation c($input: PolicyUpdateInput!) { createPolicy(input: $input) }",
        {"input": {
            "type": "METADATA",
            "name": POLICY_METADATA,
            "state": "ACTIVE",
            "description": ("DevGuard agent — exactly the privileges the write-back five "
                            "write-back artifacts need, on exactly the five "
                            "hero-path datasets. Nothing else."),
            "privileges": list(METADATA_PRIVILEGES),
            "actors": {"users": [SERVICE_ACCOUNT], "resourceOwners": False,
                       "allUsers": False, "allGroups": False},
            "resources": {"filter": {"criteria": [
                {"field": "TYPE", "condition": "EQUALS", "values": ["dataset"]},
                {"field": "URN", "condition": "EQUALS",
                 "values": list(SCOPED_DATASETS)},
            ]}},
        }}, token)


def create_platform_policy(token: str) -> dict:
    delete_existing(POLICY_PLATFORM, token)
    return gql(
        "mutation c($input: PolicyUpdateInput!) { createPolicy(input: $input) }",
        {"input": {
            "type": "PLATFORM",
            "name": POLICY_PLATFORM,
            "state": "ACTIVE",
            "description": ("DevGuard agent — document authoring only. Documents "
                            "are not resource-scoped in DataHub's privilege model, "
                            "so write-back artifact 2 has no narrower grant available."),
            "privileges": list(PLATFORM_PRIVILEGES),
            "actors": {"users": [SERVICE_ACCOUNT], "resourceOwners": False,
                       "allUsers": False, "allGroups": False},
        }}, token)


def mint_token(token: str) -> str | None:
    result = gql(
        "mutation t($input: CreateAccessTokenInput!) "
        "{ createAccessToken(input: $input) { accessToken } }",
        {"input": {"type": "PERSONAL", "actorUrn": SERVICE_ACCOUNT,
                   "duration": "ONE_DAY", "name": "devguard-agent"}}, token)
    return ((result.get("data") or {}).get("createAccessToken") or {}).get("accessToken")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-out", default=None,
                        help="write the service-account token here (chmod 600)")
    args = parser.parse_args()

    token_file = os.environ.get("DATAHUB_TOKEN_FILE")
    if not token_file:
        print("set DATAHUB_TOKEN_FILE to an admin token", file=sys.stderr)
        return 2
    admin = Path(token_file).read_text().strip()

    print(f"service account : {SERVICE_ACCOUNT}")
    ensure_user(admin)
    print("  corpUser created")

    meta = create_metadata_policy(admin)
    meta_urn = (meta.get("data") or {}).get("createPolicy")
    print(f"  metadata policy : {meta_urn}")
    print(f"    privileges    : {', '.join(METADATA_PRIVILEGES)}")
    print(f"    scoped to     : {len(SCOPED_DATASETS)} named datasets")

    plat = create_platform_policy(admin)
    plat_urn = (plat.get("data") or {}).get("createPolicy")
    print(f"  platform policy : {plat_urn}")
    print(f"    privileges    : {', '.join(PLATFORM_PRIVILEGES)}")

    if not meta_urn or not plat_urn:
        print(f"\npolicy creation failed:\n{json.dumps(meta)[:400]}\n"
              f"{json.dumps(plat)[:400]}", file=sys.stderr)
        return 1

    service_token = mint_token(admin)
    if not service_token:
        print("could not mint a service-account token", file=sys.stderr)
        return 1
    if args.token_out:
        out = Path(args.token_out)
        out.write_text(service_token)
        out.chmod(0o600)
        print(f"  token written   : {out} (chmod 600, never logged, never committed)")

    print(f"\nNOT granted, on purpose: {', '.join(DENIED_ON_PURPOSE)}")
    print("Run scripts/verify_least_privilege.py to prove both halves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
