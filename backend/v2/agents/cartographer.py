"""
cartographer.py — the Cartographer: resolve the failure to real URNs, pull schema truth.

The responsibility line: *"Resolve the failing artifact to real DataHub URNs;
pull schema truth."* Allowlist: `search`, `get_entities`, `list_schema_fields`.

The reason this agent exists as its own bounded role is that the Watcher's
output is a *string from a log* — `stg_users`, `user_id` — and everything
downstream needs a *URN*. That translation is the seam where a system quietly
starts analysing the wrong asset, so it gets its own agent, its own evidence,
and its own failure mode: if the failing artifact cannot be resolved, the
Cartographer says so rather than guessing.

Two honest notes about "schema truth":

  * The schema this reads is the **catalog's** schema, which is exactly what
    makes it useful during drift: the catalog says `user_id`, the database says
    `customer_id`, and the gap between them IS the incident. This agent must not
    "helpfully" reconcile them.
  * Every description and field-description it reads is catalog free-text and
    goes through the Sentinel before it can reach a prompt (the untrusted-content boundary).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from backend.v2.datahub_client import DataHubMCPClient
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, ToolCallRecord, now
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import ScreenedText, Sentinel


@dataclass
class ResolvedAsset:
    """A failing artifact, resolved to something DataHub can be asked about."""

    search_term: str
    urn: Optional[str] = None
    platform: Optional[str] = None
    fields: tuple[str, ...] = ()
    screened_text: tuple[ScreenedText, ...] = ()
    candidates: tuple[str, ...] = field(default=())

    @property
    def resolved(self) -> bool:
        return self.urn is not None

    def has_field(self, name: str) -> bool:
        return name in self.fields


class Cartographer:
    """Resolves names to URNs and reads the catalog's version of the schema."""

    NAME = "cartographer"

    def __init__(self, client: DataHubMCPClient, builder: EvidenceBuilder,
                 pack: ProofPack, sentinel: Optional[Sentinel] = None) -> None:
        self._client = client
        self._builder = builder
        self._pack = pack
        self._sentinel = sentinel or Sentinel()

    def resolve(self, term: str, *, prefer_platform: Optional[str] = None
                ) -> tuple[ResolvedAsset, list[Evidence], list[ToolCallRecord]]:
        records: list[ToolCallRecord] = []
        evidence: list[Evidence] = []

        search = self._client.call(self.NAME, "search", {"query": term})
        ref = self._pack.write(
            f"cartographer/search-{_slug(term)}.json",
            {"arguments": {"query": term}, "ok": search.ok,
             "text": search.text, "error": search.error},
            note="Name -> URN resolution. The seam where analysis can go to the wrong asset.",
        )
        records.append(search.record(ref))

        candidates = _dataset_urns(search.text)
        # Prefer the platform the failure actually came from. Without this the
        # dbt sibling of a postgres dataset is an equally good string match, and
        # picking the wrong one silently changes which schema is "truth".
        chosen = _choose(candidates, prefer_platform)

        asset = ResolvedAsset(search_term=term, urn=chosen,
                              candidates=tuple(candidates),
                              platform=prefer_platform)

        if chosen is None:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"search for {term!r} resolved to no dataset URN "
                      f"({len(candidates)} candidates)",
                raw_ref=ref,
            ))
            return asset, evidence, records

        evidence.append(self._builder.make(
            source=EvidenceSource.DATAHUB_GRAPH,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=f"{term!r} resolves to {chosen}",
            raw_ref=ref, datahub_urn=chosen,
        ))

        fields_result = self._client.call(self.NAME, "list_schema_fields", {"urn": chosen})
        fields_ref = self._pack.write(
            f"cartographer/schema-{_slug(term)}.json",
            {"arguments": {"urn": chosen}, "ok": fields_result.ok,
             "text": fields_result.text, "error": fields_result.error},
            note="The CATALOG's schema. During drift this deliberately disagrees "
                 "with the database, and that gap is the incident.",
        )
        records.append(fields_result.record(fields_ref))

        names, descriptions = _schema_fields(fields_result.text)
        asset.fields = tuple(names)
        asset.screened_text = self._sentinel.screen_all(
            [(f"{chosen}#{fname}", desc) for fname, desc in descriptions.items()]
        )

        if names:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"catalog schema for {term} has {len(names)} columns: "
                      f"{', '.join(names[:8])}{'…' if len(names) > 8 else ''}",
                raw_ref=fields_ref, datahub_urn=chosen,
            ))

        for screened in asset.screened_text:
            if screened.risk.value != "NONE":
                # The injection demonstration's demo beat. Logged as evidence whether or not anyone is
                # watching, because "we detected it" has to be checkable later.
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_GRAPH,
                    trust=EvidenceTrust.UNTRUSTED_TEXT,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=f"injection screen flagged {screened.field} as "
                          f"{screened.risk.value} "
                          f"({', '.join(f.pattern for f in screened.findings)})",
                    raw_ref=self._pack.write(
                        f"cartographer/screened-{_slug(screened.field)}.json",
                        {"field": screened.field, "risk": screened.risk.value,
                         "findings": [f.__dict__ | {"risk": f.risk.value}
                                      for f in screened.findings],
                         "original": screened.original},
                        note="UNTRUSTED catalog text that tripped the injection screen.",
                    ),
                    datahub_urn=chosen,
                ))

        return asset, evidence, records

    def run(self, term: str, *, prefer_platform: Optional[str] = None,
            to_agent: str = "pathfinder"):
        started = now()
        asset, evidence, records = self.resolve(term, prefer_platform=prefer_platform)
        decision = AgentDecision.OK if asset.resolved else AgentDecision.INSUFFICIENT_EVIDENCE
        rationale = (
            f"Resolved {term!r} to {asset.urn} and read {len(asset.fields)} columns "
            f"from the catalog."
            if asset.resolved else
            f"Could not resolve {term!r} to a dataset URN. Guessing which asset "
            f"failed would make every downstream conclusion unfalsifiable."
        )
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent,
            incident_id=self._builder._incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=decision, rationale=rationale,
            started_at=started, ended_at=now(),
            tokens=0, model=None, tool_calls=tuple(records),
        )
        return asset, evidence, handoff


# --------------------------------------------------------------------- parsing
#
# Kept as free functions so they are unit-testable without a live server.

def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:80]


def _walk(node) -> list:
    """Yield every dict in a nested structure. Response shapes vary by tool."""
    out = []
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            out.extend(_walk(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v))
    return out


def _dataset_urns(text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    seen: list[str] = []
    for node in _walk(payload):
        urn = node.get("urn")
        if isinstance(urn, str) and urn.startswith("urn:li:dataset:") and urn not in seen:
            seen.append(urn)
    return seen


def _choose(candidates: list[str], prefer_platform: Optional[str]) -> Optional[str]:
    if not candidates:
        return None
    if prefer_platform:
        for urn in candidates:
            if f"dataPlatform:{prefer_platform}," in urn:
                return urn
    return candidates[0]


def _schema_fields(text: str) -> tuple[list[str], dict[str, str]]:
    """Return (ordered field names, {field: description}).

    Tolerant of shape because `list_schema_fields` nests differently depending
    on whether the dataset has siblings.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [], {}
    names: list[str] = []
    descriptions: dict[str, str] = {}
    for node in _walk(payload):
        path = node.get("fieldPath") or node.get("path") or node.get("name")
        if isinstance(path, str) and path and path not in names:
            if not isinstance(node.get("description", ""), (str, type(None))):
                continue
            names.append(path)
            if node.get("description"):
                descriptions[path] = node["description"]
    return names, descriptions
