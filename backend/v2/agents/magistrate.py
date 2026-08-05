"""
magistrate.py — the Magistrate: risk class, owners from the graph, approval routing.

Role: *"Magistrate | deterministic + LLM | `get_entities` (owners) read-only |
Risk classification, autonomy policy, owner-routed approval."*

The autonomy policy requires *"a written table: risk class → allowed action → who approves.
HIGH/CRITICAL always requires the named owner."* `AUTONOMY_POLICY` below **is**
that table — the same object the docs render and the code branches on, so the
published policy and the enforced policy cannot drift apart.

Two decisions are deliberate:

**Owners come from the graph, not from config.** step 14 says "owners from
the graph → approval routed by real name". If the asset has no owner, that is
itself a finding: an unowned production table with a live incident is a
governance gap, and write-back artifact 5 exists to close it. The Magistrate reports
`UNOWNED` rather than silently routing to a default approver.

**Approval is recorded, never inferred.** `Approval` carries an identity, a
timestamp and a decision, and `ApprovalRequest.approve()` requires all three.
There is no auto-approve path for HIGH or CRITICAL, and `CRITICAL` has no
approver at all — the autonomy policy's table has no row that permits it, so neither does the
code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from backend.v2.datahub_client import DataHubMCPClient
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, ToolCallRecord, now
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import PatchRisk


class ApprovalMode(str, Enum):
    AUTONOMOUS = "AUTONOMOUS"
    """DevGuard may proceed and record what it did."""

    NAMED_OWNER = "NAMED_OWNER"
    """A specific human, resolved from the graph, must approve."""

    FORBIDDEN = "FORBIDDEN"
    """No approver exists. The change does not proceed."""


#: The autonomy policy's autonomy policy. The published table and the enforced table are the
#: same object on purpose — a policy rendered from prose in a README is a policy
#: that drifts from the code within a week.
AUTONOMY_POLICY: dict[PatchRisk, dict] = {
    PatchRisk.LOW: {
        "mode": ApprovalMode.NAMED_OWNER,
        "allowed_action": "propose + validate; apply only after owner approval",
        "approver": "asset owner from the DataHub graph",
        "rationale": ("Even a one-line fix changes a production model. DevGuard "
                      "proposes and proves; a human decides."),
    },
    PatchRisk.MEDIUM: {
        "mode": ApprovalMode.NAMED_OWNER,
        "allowed_action": "propose + validate; apply only after owner approval",
        "approver": "asset owner from the DataHub graph",
        "rationale": "Multiple files or configuration touched — the blast radius is wider.",
    },
    PatchRisk.HIGH: {
        "mode": ApprovalMode.NAMED_OWNER,
        "allowed_action": "propose + validate; apply only after owner approval",
        "approver": "asset owner from the DataHub graph (the autonomy policy: always named)",
        "rationale": ("Filters or semantics may have changed; a green build does "
                      "not mean the numbers are right."),
    },
    PatchRisk.CRITICAL: {
        "mode": ApprovalMode.FORBIDDEN,
        "allowed_action": "nothing is applied; the proposal is recorded and escalated",
        "approver": None,
        "rationale": ("Destructive DDL, data mutation, permission changes or a "
                      "hard-coded credential. No approval path exists for an "
                      "automated agent to take these actions."),
    },
}

#: DevGuard never runs autonomously in this build. Kept explicit so that a future
#: change to enable it is a visible diff rather than a silent default.
assert all(p["mode"] is not ApprovalMode.AUTONOMOUS for p in AUTONOMY_POLICY.values())


@dataclass(frozen=True)
class Owner:
    urn: str
    display_name: str
    ownership_type: Optional[str] = None


@dataclass(frozen=True)
class Approval:
    """A recorded human decision. Never inferred, never defaulted."""

    approved: bool
    approver_urn: str
    approver_name: str
    decided_at_utc: str
    note: str = ""


@dataclass
class ApprovalRequest:
    """step 14–15: routed to a real name, and awaiting a real answer."""

    incident_id: str
    risk: PatchRisk
    mode: ApprovalMode
    allowed_action: str
    owners: tuple[Owner, ...]
    target_urn: str
    approval: Optional[Approval] = None

    @property
    def unowned(self) -> bool:
        return not self.owners

    @property
    def can_proceed(self) -> bool:
        return (self.mode is not ApprovalMode.FORBIDDEN
                and self.approval is not None
                and self.approval.approved)

    def approve(self, *, approver_urn: str, approver_name: str,
                note: str = "") -> "ApprovalRequest":
        if self.mode is ApprovalMode.FORBIDDEN:
            raise PermissionError(
                f"{self.risk.value} changes have no approval path (the autonomy policy); "
                f"this request cannot be approved by anyone.")
        if not approver_urn or not approver_name:
            raise ValueError("approval requires a real identity — the autonomy policy says "
                             "'routed by real name'")
        self.approval = Approval(
            approved=True, approver_urn=approver_urn, approver_name=approver_name,
            decided_at_utc=datetime.now(timezone.utc).isoformat(), note=note)
        return self

    def reject(self, *, approver_urn: str, approver_name: str,
               note: str = "") -> "ApprovalRequest":
        self.approval = Approval(
            approved=False, approver_urn=approver_urn, approver_name=approver_name,
            decided_at_utc=datetime.now(timezone.utc).isoformat(), note=note)
        return self


class Magistrate:
    """Classifies, resolves owners read-only, and routes. Never applies anything."""

    NAME = "magistrate"

    def __init__(self, client: DataHubMCPClient, builder: EvidenceBuilder,
                 pack: ProofPack) -> None:
        self._client = client
        self._builder = builder
        self._pack = pack

    def owners_of(self, urn: str) -> tuple[tuple[Owner, ...], ToolCallRecord, str]:
        result = self._client.call(self.NAME, "get_entities", {"urns": [urn]})
        raw_ref = self._pack.write(
            "magistrate/get_entities.json",
            {"arguments": {"urns": [urn]}, "ok": result.ok, "text": result.text,
             "error": result.error},
            note="Owner lookup — read-only, from the graph.",
        )
        return _owners(result.text), result.record(raw_ref), raw_ref

    def route(self, incident_id: str, risk: PatchRisk, target_urn: str
              ) -> tuple[ApprovalRequest, list[Evidence], list[ToolCallRecord]]:
        policy = AUTONOMY_POLICY[risk]
        owners, record, raw_ref = self.owners_of(target_urn)

        request = ApprovalRequest(
            incident_id=incident_id, risk=risk, mode=policy["mode"],
            allowed_action=policy["allowed_action"], owners=owners,
            target_urn=target_urn,
        )

        evidence = [self._builder.make(
            source=EvidenceSource.DATAHUB_GRAPH,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=(f"{len(owners)} owner(s) on {target_urn.split(',')[1] if ',' in target_urn else target_urn}"
                   + (f": {', '.join(o.display_name for o in owners)}" if owners
                      else " — UNOWNED, approval cannot be routed to a named owner")),
            raw_ref=raw_ref, datahub_urn=target_urn,
        ), self._builder.make(
            source=EvidenceSource.DEVGUARD_INFERENCE,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.DERIVED,
            claim=f"risk {risk.value} -> {policy['mode'].value}: {policy['allowed_action']}",
            raw_ref=self._pack.write(
                "magistrate/autonomy-decision.json",
                {"risk": risk.value, "mode": policy["mode"].value,
                 "allowed_action": policy["allowed_action"],
                 "approver": policy["approver"], "rationale": policy["rationale"],
                 "owners": [o.__dict__ for o in owners], "target_urn": target_urn},
                note="The autonomy policy's policy table, as applied to this change."),
            datahub_urn=target_urn,
        )]
        return request, evidence, [record]

    def run(self, incident_id: str, risk: PatchRisk, target_urn: str, *,
            to_agent: str = "human"):
        started = now()
        request, evidence, records = self.route(incident_id, risk, target_urn)
        if request.mode is ApprovalMode.FORBIDDEN:
            decision, rationale = AgentDecision.INSUFFICIENT_EVIDENCE, (
                f"{risk.value}: {AUTONOMY_POLICY[risk]['rationale']}")
        elif request.unowned:
            decision, rationale = AgentDecision.DEGRADED, (
                f"{risk.value} requires a named owner and this asset has none. "
                f"Escalating as a governance gap rather than routing to a default.")
        else:
            decision, rationale = AgentDecision.OK, (
                f"{risk.value} routed to {', '.join(o.display_name for o in request.owners)}.")
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent, incident_id=incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=decision, rationale=rationale,
            started_at=started, ended_at=now(), tokens=0, model=None,
            tool_calls=tuple(records),
        )
        return request, evidence, handoff


def _owners(text: str) -> tuple[Owner, ...]:
    """Pull owners out of a get_entities response, tolerant of nesting."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ()
    found: dict[str, Owner] = {}
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            owner = node.get("owner")
            if isinstance(owner, dict) and isinstance(owner.get("urn"), str):
                urn = owner["urn"]
                props = owner.get("properties") or {}
                name = (props.get("displayName") or props.get("fullName")
                        or owner.get("username") or urn.rsplit(":", 1)[-1])
                otype = node.get("type") or (node.get("ownershipType") or {}).get("urn")
                found.setdefault(urn, Owner(urn=urn, display_name=str(name),
                                            ownership_type=otype))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return tuple(found.values())
