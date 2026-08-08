#!/usr/bin/env python
"""
provision_catalog.py — the catalog structures DevGuard writes *into*.

    DATAHUB_GMS_URL=http://localhost:8080 python scripts/provision_catalog.py

Two things live here, and they are together for one reason: both must exist
**before** an agent runs, and neither may be created by the agent itself.

  1. The **Domain** the substrate's assets belong to.
  2. The **tag vocabulary** the Scribe applies during write-back.

Why the agent does not create its own tag
-----------------------------------------
`backend/v2/agents/scribe.py` refuses to mint a missing tag, and says why:
*"silently minting tags from an automated agent is how a catalog's vocabulary
rots."* DataHub agrees — `add_tags` against a tag URN that does not exist fails
with `Failed to validate label ... Urn does not exist`, which is precisely the
error a live run produced before this file existed.

That refusal is correct and stays. The consequence is that the vocabulary is an
**operator responsibility**, declared here, reviewable in a diff, and applied
once at provisioning time. An agent that can invent vocabulary can invent
meaning; an agent that can only apply a term someone already approved cannot.

Why the Domain does NOT change the policy scope
------------------------------------------------
`scripts/setup_service_account.py` carries a deliberate, documented decision:
the least-privilege policy is scoped to **an explicit allowlist of dataset
URNs**, not to a domain, because "a domain would grant access to anything later
added to it, this grants access to five named datasets and nothing else."

That decision stands and this script does not touch it. A Domain here is an
*organisational* grouping — the thing DataHub's browse, search facets and
governance views are built around — not an authorisation boundary. The two were
conflated in the earlier note only because no domain existed; now that one does,
the distinction is worth stating plainly:

  * The **Domain** answers "what is this asset part of?"  (this file)
  * The **Access Policy** answers "what may the agent write?" (setup_service_account.py)

Widening the policy to the domain would re-introduce exactly the risk that note
rejected, so it is not done, and `scripts/verify_least_privilege.py` still
proves the URN allowlist empirically.

Idempotent: re-running it re-asserts the same aspects.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Optional

from datahub.emitter.mce_builder import make_dataset_urn, make_user_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    DomainPropertiesClass,
    DomainsClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    TagPropertiesClass,
)

DOMAIN_ID = "devguard_substrate"
DOMAIN_URN = f"urn:li:domain:{DOMAIN_ID}"

#: The tags DevGuard is permitted to apply, and their meaning. The Scribe's
#: `IMPACT_TAG` must appear here or artifact 3 of the write-back fails at the
#: server — which is the designed behaviour, not a bug to route around.
TAG_VOCABULARY: dict[str, str] = {
    "devguard_incident_impacted": (
        "Applied by DevGuard to a schema field that a verified incident touched. "
        "Written only after the Referee confirms recovery, so its presence means "
        "'this column was involved in an incident that was diagnosed and fixed' — "
        "never 'something might be wrong here'."
    ),
}

#: Both platform representations of each hero table. The postgres URN is the
#: physical table; the dbt URN is the transformation node describing it. They
#: are siblings in DataHub and a domain set on only one of them makes the pair
#: disagree in search facets.
_TABLES = (
    "devguard.raw.users",
    "devguard.raw.orders",
    "devguard.analytics_staging.stg_users",
    "devguard.analytics_staging.stg_orders",
    "devguard.analytics_marts.user_order_features",
)
HERO_DATASETS = tuple(
    make_dataset_urn(platform, name, "PROD")
    for name in _TABLES
    for platform in ("postgres", "dbt")
)


def build_mcps() -> list[MetadataChangeProposalWrapper]:
    mcps = [
        MetadataChangeProposalWrapper(
            entityUrn=DOMAIN_URN,
            aspect=DomainPropertiesClass(
                name="DevGuard Substrate",
                description=(
                    "The demonstration data platform DevGuard reasons over: a real "
                    "PostgreSQL, real dbt models built from it, and the churn model "
                    "trained on the resulting feature table. Created by "
                    "scripts/provision_domain.py. This domain groups assets for "
                    "browse and governance; it is deliberately NOT the scope of the "
                    "agent's write policy, which is an explicit URN allowlist."
                ),
            ),
        ),
        MetadataChangeProposalWrapper(
            entityUrn=DOMAIN_URN,
            aspect=OwnershipClass(owners=[
                OwnerClass(owner=make_user_urn("datahub"),
                           type=OwnershipTypeClass.TECHNICAL_OWNER),
            ]),
        ),
    ]
    mcps += [
        MetadataChangeProposalWrapper(
            entityUrn=urn, aspect=DomainsClass(domains=[DOMAIN_URN]),
        )
        for urn in HERO_DATASETS
    ]
    mcps += [
        MetadataChangeProposalWrapper(
            entityUrn=f"urn:li:tag:{tag_id}",
            aspect=TagPropertiesClass(name=tag_id, description=description),
        )
        for tag_id, description in TAG_VOCABULARY.items()
    ]
    return mcps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mcps = build_mcps()
    print(f"domain   {DOMAIN_URN}")
    print(f"assets   {len(HERO_DATASETS)} dataset URNs "
          f"({len(_TABLES)} tables × postgres/dbt siblings)")
    print(f"tags     {', '.join(sorted(TAG_VOCABULARY))}")
    print()

    if args.dry_run:
        for mcp in mcps:
            print(f"  {type(mcp.aspect).__name__:22} -> {mcp.entityUrn}")
        print("\n  dry run: nothing emitted")
        return 0

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token: Optional[str] = os.environ.get("DATAHUB_GMS_TOKEN") or None
    token_file = os.environ.get("DATAHUB_TOKEN_FILE")
    if not token and token_file:
        token = pathlib.Path(token_file).read_text().strip()

    emitter = DatahubRestEmitter(gms_server=gms, token=token)
    for mcp in mcps:
        emitter.emit(mcp)
    print(f"  {len(mcps)} aspects emitted to {gms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
