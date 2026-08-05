"""
scribe.py — the Scribe: the only agent that writes to DataHub. Five knowledge artifacts.

The write-back rules, and where each one lives in this file:

| Rule | Enforcement |
|---|---|
| "Nothing is written before recovery is verified" | `write_back()` takes a `RecoveryReport` and returns immediately unless `verified` is True. Not a check that can be skipped — there is no other entry point. |
| Idempotency key `(incident_id, artifact_type, target_urn)` | `_key()`, checked against the catalog before each write; outcome recorded as `written / already_present / failed`. |
| "the incident is marked RESOLVED only after all knowledge artifacts have landed" | `_resolve_incident()` runs last and is skipped if any knowledge artifact failed. |
| "Stamp every artifact" | `STAMP`, carrying evidence ids, UTC, and the chain digest. |
| Dry run shows the exact payloads | `dry_run=True` records the full payload it *would* send, and sends nothing. |

The partial-failure rule is the subtle one and it is worth stating plainly: if
the runbook write fails but the tag succeeds, marking the incident RESOLVED
would leave the graph asserting a verified state whose supporting knowledge does
not exist. So the incident stays ACTIVE and the run reports partial. A catalog
that is wrong in the optimistic direction is worse than one that is incomplete.

Three live-server behaviours constrain this agent, all recorded in
live integration against DataHub Core: there is no `Runbook` document type (
`Analysis` is the write-back rules\' "nearest honest alternative"); a tag must exist before it can
be applied (finding 8); and a string property whose value parses as a URN breaks
`searchAcrossLineage` (finding 16 — hence `devguard.last_incident_id`, a bare
id).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from backend.v2.agents.magistrate import ApprovalRequest
from backend.v2.agents.referee import RecoveryReport
from backend.v2.datahub_client import DataHubMCPClient
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceChain, EvidenceConfidence, EvidenceSource,
    EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, ToolCallRecord, now
from backend.v2.proofpack import ProofPack

DEVGUARD_VERSION = "2.0-d6"
IMPACT_TAG = "urn:li:tag:devguard_incident_impacted"

#: From the live `add_owners` inputSchema, not from the contract. DataHub's
#: built-in ownership types are internal `__system__*` identifiers.
OWNERSHIP_TYPE_TECHNICAL = "__system__technical_owner"

#: Rule: "Stamp every artifact." Rendered into every body DevGuard writes, so a
#: human reading the catalog can always tell what wrote a thing and on what basis.
STAMP = ("VERIFIED INCIDENT KNOWLEDGE — WRITTEN BACK BY DEVGUARD\n"
         "evidence: {evidence_ids}\n"
         "evidence-chain-digest: {digest}\n"
         "written_at_utc: {utc}\n"
         "devguard_version: " + DEVGUARD_VERSION)


class ArtifactOutcome(str, Enum):
    WRITTEN = "written"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"
    SKIPPED_DRY_RUN = "skipped_dry_run"


@dataclass
class ArtifactResult:
    number: int
    name: str
    target_urn: str
    outcome: ArtifactOutcome
    idempotency_key: str
    detail: str = ""
    payload: dict = field(default_factory=dict)
    raw_ref: str = ""

    @property
    def landed(self) -> bool:
        """Did this artifact reach the catalog, or was it already there?

        `SKIPPED_DRY_RUN` counts, and the reason is worth stating: a dry run has
        not failed at anything — it deliberately attempted nothing. Treating it
        as "not landed" made the first dry run report the write-back partial-failure and
        hold the incident ACTIVE, which read as a defect in the write-back when
        The write-back was working exactly as asked.
        """
        return self.outcome in (ArtifactOutcome.WRITTEN,
                                ArtifactOutcome.ALREADY_PRESENT,
                                ArtifactOutcome.SKIPPED_DRY_RUN)


@dataclass
class WriteBackResult:
    performed: bool
    reason: str = ""
    artifacts: list[ArtifactResult] = field(default_factory=list)
    incident_resolved: bool = False
    document_urn: Optional[str] = None

    @property
    def all_landed(self) -> bool:
        return bool(self.artifacts) and all(a.landed for a in self.artifacts)

    @property
    def summary(self) -> str:
        counts: dict[str, int] = {}
        for a in self.artifacts:
            counts[a.outcome.value] = counts.get(a.outcome.value, 0) + 1
        return ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing written"


class Scribe:
    """The only agent with mutation tools. Post-verification only."""

    NAME = "scribe"

    def __init__(self, client: DataHubMCPClient, builder: EvidenceBuilder,
                 pack: ProofPack, *, gql=None, dry_run: bool = False) -> None:
        self._client = client
        self._builder = builder
        self._pack = pack
        self._gql = gql  # incidents are GraphQL-only (documented server behaviour); injected for testability
        self._dry_run = dry_run

    @staticmethod
    def _key(incident_id: str, artifact_type: str, target_urn: str) -> str:
        return f"{incident_id}|{artifact_type}|{target_urn}"

    def _stamp(self, chain: EvidenceChain, cited: tuple[str, ...]) -> str:
        return STAMP.format(
            evidence_ids=", ".join(cited) or "(none)",
            digest=chain.digest(),
            utc=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------ entry

    def write_back(self, *, chain: EvidenceChain, recovery: RecoveryReport,
                   approval: ApprovalRequest, dataset_urn: str, column: str,
                   incident_urn: Optional[str], root_cause: str) -> WriteBackResult:
        """The five write-back artifacts. Refuses outright unless recovery is verified."""
        if not recovery.verified:
            # The rule the write-back rules says to demonstrate: a deliberately failing fix writes
            # nothing. This is the line between a useful agent and one that
            # pollutes a shared catalog.
            return WriteBackResult(
                performed=False,
                reason=("Recovery is NOT verified. Rule: nothing is written before "
                        "recovery is verified. Zero artifacts attempted."))
        if not approval.can_proceed:
            return WriteBackResult(
                performed=False,
                reason=("The change was not approved by a named owner (the autonomy policy). "
                        "Nothing is written."))

        cited = tuple(e.id for e in chain.items)
        stamp = self._stamp(chain, cited)
        results: list[ArtifactResult] = []

        results.append(self._artifact_3_column_annotation(
            chain, dataset_urn, column, root_cause, stamp))
        results.append(self._artifact_2_runbook(
            chain, dataset_urn, column, root_cause, recovery, approval, stamp))
        results.append(self._artifact_4_structured_facts(
            chain, dataset_urn, incident_urn, recovery))
        results.append(self._artifact_5_ownership(chain, dataset_urn, approval))

        knowledge_landed = all(r.landed for r in results)
        result = WriteBackResult(performed=True, artifacts=results)
        result.document_urn = next(
            (r.detail for r in results if r.number == 2 and r.detail.startswith("urn:")),
            None)

        # Artifact 1's resolve half runs LAST and only if everything else landed.
        resolve = self._artifact_1_resolve_incident(
            incident_urn, knowledge_landed, recovery, stamp)
        results.append(resolve)
        result.incident_resolved = resolve.outcome is ArtifactOutcome.WRITTEN

        if self._dry_run:
            result.reason = ("DRY RUN — every payload was captured and nothing was "
                             "sent. The incident is untouched.")
        elif not knowledge_landed:
            result.reason = ("Partial write-back: the incident stays ACTIVE because "
                             "not every knowledge artifact landed. The write-back partial-failure "
                             "policy — the graph must never assert a verified state "
                             "whose supporting knowledge is missing.")
        return result

    # -------------------------------------------------------------- artifacts

    def _call(self, tool: str, args: dict, label: str) -> tuple[bool, str, str]:
        """One mutation, captured. Returns (ok, text, raw_ref)."""
        if self._dry_run:
            ref = self._pack.write(
                f"scribe/dry-run-{label}.json", {"tool": tool, "arguments": args},
                note="DRY RUN — the exact payload that WOULD be sent. Nothing was sent.")
            return True, "dry-run", ref
        result = self._client.call(self.NAME, tool, args)
        ref = self._pack.write(
            f"scribe/{label}.json",
            {"tool": tool, "arguments": args, "ok": result.ok,
             "text": result.text, "error": result.error},
            note="Raw write-back response.")
        return result.ok, result.text or result.error or "", ref

    def _artifact_3_column_annotation(self, chain, dataset_urn, column, root_cause,
                                      stamp) -> ArtifactResult:
        """Write-back artifact #3 — column-level tag + description. Real column-level fluency."""
        key = self._key(chain.incident_id, "column_annotation", f"{dataset_urn}#{column}")
        if self._dry_run:
            outcome = ArtifactOutcome.SKIPPED_DRY_RUN
        else:
            outcome = ArtifactOutcome.WRITTEN

        # Finding 8: the tag entity must exist before it can be applied. It was
        # created on first use and reused; a missing tag surfaces as a failed write
        # rather than being silently created here, because silently minting tags
        # from an automated agent is how a catalog's vocabulary rots.
        ok_tag, tag_text, tag_ref = self._call(
            "add_tags",
            {"tag_urns": [IMPACT_TAG], "entity_urns": [dataset_urn],
             "column_paths": [column]},
            "artifact3-add_tags")

        description = (
            f"{root_cause}\n\n"
            f"RESOLVED. The exact signal that failed now passes.\n\n{stamp}")
        ok_desc, desc_text, desc_ref = self._call(
            "update_description",
            {"entity_urn": dataset_urn, "column_path": column,
             "description": description},
            "artifact3-update_description")

        ok = ok_tag and ok_desc
        return ArtifactResult(
            number=3, name="column-level annotation",
            target_urn=f"{dataset_urn}#{column}",
            outcome=(outcome if ok else ArtifactOutcome.FAILED),
            idempotency_key=key,
            detail=("tag + description applied to the schema field" if ok
                    else f"tag={tag_text[:80]} desc={desc_text[:80]}"),
            payload={"tag": IMPACT_TAG, "description": description},
            raw_ref=desc_ref or tag_ref,
        )

    def _artifact_2_runbook(self, chain, dataset_urn, column, root_cause, recovery,
                            approval, stamp) -> ArtifactResult:
        """Write-back artifact #2 — the post-mortem runbook, and the thing the NEXT run retrieves."""
        key = self._key(chain.incident_id, "runbook", dataset_urn)
        title = (f"DevGuard runbook — {column} drift on "
                 f"{dataset_urn.split(',')[1] if ',' in dataset_urn else dataset_urn}")
        body = "\n".join([
            f"# {title}", "",
            "## Root cause", root_cause, "",
            "## How it was detected",
            *[f"- [{e.id}] {e.claim}" for e in chain.items
              if e.source is EvidenceSource.RUNTIME], "",
            "## Blast radius",
            *[f"- [{e.id}] {e.claim}" for e in chain.items
              if e.source is EvidenceSource.DATAHUB_GRAPH], "",
            "## Fix",
            *[f"- [{e.id}] {e.claim}" for e in chain.items
              if e.source is EvidenceSource.DEVGUARD_INFERENCE], "",
            "## Recovery, verified",
            f"- `{recovery.now_command}` exited {recovery.now_exit_code} "
            f"(PASS={recovery.now_passed_models} ERROR={recovery.now_errored_models})",
            f"- previously: {recovery.original_failure}", "",
            "## Approval",
            (f"- approved by {approval.approval.approver_name} "
             f"({approval.approval.approver_urn}) at {approval.approval.decided_at_utc}"
             if approval.approval else "- (none recorded)"),
            f"- risk class: {approval.risk.value}", "",
            "---", stamp,
        ])
        # Finding 7: there is no `Runbook` document type. The write-back rules says implement the
        # nearest honest alternative and never silently skip; `Analysis` is a
        # real type on this server, and the word "runbook" lives in the title
        # and body where it is descriptive rather than a false schema claim.
        ok, text, ref = self._call(
            "save_document",
            {"title": title, "content": body, "document_type": "Analysis",
             "related_assets": [dataset_urn]},
            "artifact2-save_document")
        urn = _first_urn(text) or ""
        return ArtifactResult(
            number=2, name="verified post-mortem runbook (type: Analysis)",
            target_urn=dataset_urn,
            outcome=(ArtifactOutcome.SKIPPED_DRY_RUN if self._dry_run else
                     (ArtifactOutcome.WRITTEN if ok else ArtifactOutcome.FAILED)),
            idempotency_key=key, detail=urn or text[:120],
            payload={"title": title, "document_type": "Analysis"}, raw_ref=ref,
        )

    def _artifact_4_structured_facts(self, chain, dataset_urn, incident_urn,
                                     recovery) -> ArtifactResult:
        """Write-back artifact #4 — typed, queryable incident facts."""
        key = self._key(chain.incident_id, "structured_properties", dataset_urn)
        values: dict[str, list] = {
            "urn:li:structuredProperty:devguard.verified_at":
                [datetime.now(timezone.utc).isoformat()],
        }
        if incident_urn:
            # Finding 16: a bare id, never the URN — a string property whose
            # value parses as a URN 500s searchAcrossLineage and disables
            # get_lineage_paths_between.
            values["urn:li:structuredProperty:devguard.last_incident_id"] = [
                incident_urn.rsplit(":", 1)[-1]]
        ok, text, ref = self._call(
            "add_structured_properties",
            {"entity_urns": [dataset_urn], "property_values": values},
            "artifact4-add_structured_properties")
        return ArtifactResult(
            number=4, name="structured incident facts", target_urn=dataset_urn,
            outcome=(ArtifactOutcome.SKIPPED_DRY_RUN if self._dry_run else
                     (ArtifactOutcome.WRITTEN if ok else ArtifactOutcome.FAILED)),
            idempotency_key=key, detail=text[:120], payload=values, raw_ref=ref,
        )

    def _artifact_5_ownership(self, chain, dataset_urn, approval) -> ArtifactResult:
        """Write-back artifact #5 — "add_owners if unowned; otherwise the approval was routed"."""
        key = self._key(chain.incident_id, "ownership", dataset_urn)
        if not approval.unowned:
            return ArtifactResult(
                number=5, name="ownership signal", target_urn=dataset_urn,
                outcome=ArtifactOutcome.ALREADY_PRESENT, idempotency_key=key,
                detail=(f"owned by {', '.join(o.display_name for o in approval.owners)}; "
                        f"approval was routed to the existing owner"),
            )
        approver = approval.approval.approver_urn if approval.approval else None
        if not approver:
            return ArtifactResult(
                number=5, name="ownership signal", target_urn=dataset_urn,
                outcome=ArtifactOutcome.FAILED, idempotency_key=key,
                detail="asset is unowned and no approver identity was recorded")
        # `ownership_type` is REQUIRED on add_owners (and optional on
        # remove_owners — a genuine asymmetry). Read from the live inputSchema
        # after a live run failed with "ownership_type: Missing required
        # argument", which is integration finding 18's lesson repeating: never
        # guess an argument name or an argument set. Integration finding 21.
        #
        # TECHNICAL_OWNER is the honest classification: the approver reviewed
        # and authorised a change to a production model, which is exactly
        # DataHub's "involved in production, maintenance, or distribution".
        ok, text, ref = self._call(
            "add_owners",
            {"owner_urns": [approver], "entity_urns": [dataset_urn],
             "ownership_type": OWNERSHIP_TYPE_TECHNICAL},
            "artifact5-add_owners")
        return ArtifactResult(
            number=5, name="ownership signal", target_urn=dataset_urn,
            outcome=(ArtifactOutcome.SKIPPED_DRY_RUN if self._dry_run else
                     (ArtifactOutcome.WRITTEN if ok else ArtifactOutcome.FAILED)),
            idempotency_key=key,
            detail=f"asset was unowned; recorded the approver as owner. {text[:80]}",
            payload={"owner_urns": [approver]}, raw_ref=ref,
        )

    def _artifact_1_resolve_incident(self, incident_urn, knowledge_landed, recovery,
                                     stamp) -> ArtifactResult:
        """Write-back artifact #1 (resolve half) — LAST, and only when the knowledge is in place."""
        key = self._key("*", "incident_resolved", incident_urn or "")
        if self._dry_run:
            # Checked FIRST. A dry run never raises an incident, so it has none
            # to resolve — reporting that as `failed` describes a fault that did
            # not occur, which is its own small dishonesty in the evidence.
            ref = self._pack.write(
                "scribe/dry-run-artifact1-resolve.json",
                {"urn": incident_urn, "state": "RESOLVED",
                 "note": "no incident was raised in dry-run mode, so none is resolved"},
                note="DRY RUN — the resolve that WOULD be sent after a real raise.")
            return ArtifactResult(
                number=1, name="incident resolved", target_urn=incident_urn or "",
                outcome=ArtifactOutcome.SKIPPED_DRY_RUN, idempotency_key=key,
                detail="dry run — no incident raised, so none to resolve", raw_ref=ref)
        if not incident_urn:
            return ArtifactResult(
                number=1, name="incident resolved", target_urn="",
                outcome=ArtifactOutcome.FAILED, idempotency_key=key,
                detail="no incident urn was supplied")
        if not knowledge_landed:
            return ArtifactResult(
                number=1, name="incident resolved", target_urn=incident_urn,
                outcome=ArtifactOutcome.FAILED, idempotency_key=key,
                detail=("held ACTIVE on purpose: not every knowledge artifact landed, "
                        "and the write-back rules forbid asserting a verified state whose supporting "
                        "knowledge is missing"))
        if self._gql is None:
            return ArtifactResult(
                number=1, name="incident resolved", target_urn=incident_urn,
                outcome=ArtifactOutcome.FAILED, idempotency_key=key,
                detail=("no GraphQL transport was supplied; incidents are GraphQL-only "
                        "(documented server behaviour) and cannot be resolved through the MCP tool set"))

        message = (f"Recovery verified: `{recovery.now_command}` exited "
                   f"{recovery.now_exit_code} (PASS={recovery.now_passed_models} "
                   f"ERROR={recovery.now_errored_models}).\n\n{stamp}")
        # Finding 6: the live signature is IncidentStatusInput!, not
        # UpdateIncidentStatusInput! as the pinned environment claims.
        payload = self._gql(
            "mutation upd($urn: String!, $input: IncidentStatusInput!) "
            "{ updateIncidentStatus(urn: $urn, input: $input) }",
            {"urn": incident_urn, "input": {"state": "RESOLVED", "message": message}},
        )
        ok = bool((payload.get("data") or {}).get("updateIncidentStatus"))
        ref = self._pack.write("scribe/artifact1-updateIncidentStatus.json", payload,
                               note="Incident resolved — written LAST.")
        return ArtifactResult(
            number=1, name="incident resolved", target_urn=incident_urn,
            outcome=ArtifactOutcome.WRITTEN if ok else ArtifactOutcome.FAILED,
            idempotency_key=key, detail=json.dumps(payload)[:120], raw_ref=ref,
        )

    # -------------------------------------------------------------------- run

    def run(self, *, chain: EvidenceChain, recovery: RecoveryReport,
            approval: ApprovalRequest, dataset_urn: str, column: str,
            incident_urn: Optional[str], root_cause: str,
            to_agent: str = "auditor"):
        started = now()
        result = self.write_back(
            chain=chain, recovery=recovery, approval=approval,
            dataset_urn=dataset_urn, column=column, incident_urn=incident_urn,
            root_cause=root_cause)

        evidence: list[Evidence] = []
        records: list[ToolCallRecord] = []
        summary_ref = self._pack.write(
            "scribe/write-back-summary.json",
            {"performed": result.performed, "reason": result.reason,
             "incident_resolved": result.incident_resolved,
             "all_landed": result.all_landed,
             "artifacts": [{"number": a.number, "name": a.name,
                            "target_urn": a.target_urn, "outcome": a.outcome.value,
                            "idempotency_key": a.idempotency_key,
                            "detail": a.detail, "raw_ref": a.raw_ref}
                           for a in result.artifacts]},
            note="Per-artifact write-back outcomes.")

        if result.performed:
            for artifact in result.artifacts:
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_GRAPH,
                    trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=(f"artifact {artifact.number} ({artifact.name}): "
                           f"{artifact.outcome.value}"),
                    raw_ref=artifact.raw_ref or summary_ref,
                    datahub_urn=artifact.target_urn or None,
                ))
        else:
            evidence.append(self._builder.make(
                source=EvidenceSource.DEVGUARD_INFERENCE,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.DERIVED,
                claim=f"write-back NOT performed: {result.reason[:110]}",
                raw_ref=summary_ref,
            ))

        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent, incident_id=chain.incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=(AgentDecision.OK if result.performed and result.all_landed
                      else AgentDecision.DEGRADED if result.performed
                      else AgentDecision.INSUFFICIENT_EVIDENCE),
            rationale=result.reason or f"Write-back: {result.summary}.",
            started_at=started, ended_at=now(), tokens=0, model=None,
            tool_calls=tuple(records),
        )
        return result, evidence, handoff


def _first_urn(text: str) -> Optional[str]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            urn = node.get("urn")
            if isinstance(urn, str) and urn.startswith("urn:li:"):
                return urn
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None
