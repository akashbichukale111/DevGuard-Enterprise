"""
archivist.py — the Archivist: capability negotiation, then prior knowledge.

Two responsibilities, and the first one is the interesting one.

**Capability negotiation** (step 4). The pinned environment's trap: *"`search_documents` /
`grep_documents` are **automatically hidden when the catalog has no documents**
— i.e. on a clean instance and during incident #1. Code must negotiate
capabilities and degrade deliberately, never throw."*

So the Archivist's job on incident #1 is to *not find anything*, and to say so
in a way that is distinguishable from an error. That distinction is the whole
point: "there is no prior runbook for this" and "the document search broke" lead
to completely different downstream behaviour, and a system that conflates them
will happily report a clean bill of health when its retrieval is down.

`AgentDecision.DEGRADED` carries that distinction, and the captured tool list confirms the
trap is real: the live server offered 18 tools, and both document tools were
among the absent ones (integration finding 4).

**Prior knowledge.** Anything the documents *do* return is human-authored text
from a shared catalog, so it is `DATAHUB_DOCUMENT` + `UNTRUSTED_TEXT` by
construction — the Evidence model refuses to let it be anything else — and it
goes through the Sentinel before any agent reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from backend.v2.datahub_client import Capabilities, DataHubMCPClient
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import (
    AGENT_TOOL_ALLOWLISTS, AgentDecision, AgentHandoff, ToolCallRecord, now,
)
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import ScreenedText, Sentinel

DOCUMENT_TOOLS = frozenset({"search_documents", "grep_documents"})


@dataclass(frozen=True)
class PriorKnowledge:
    """What the Archivist found, and — equally important — how hard it could look."""

    documents_available: bool
    """False when the catalog has no documents and the tools are hidden."""

    documents: tuple[ScreenedText, ...]
    negotiated_tools: frozenset[str]
    missing_from_contract: frozenset[str]

    retrieval_enabled: bool = True
    """False only when the retrieval ablation's ablation switched retrieval off deliberately.

    Kept separate from `documents_available` on purpose: "we chose not to look"
    and "we could not look" are different facts, and collapsing them would make
    the ablation's off-arm indistinguishable from a broken catalog.
    """

    @property
    def has_prior_incident(self) -> bool:
        return bool(self.documents)

    @property
    def summary(self) -> str:
        """The write-back 'PREVIOUS VERIFIED INCIDENT' line, or an explicit miss."""
        if not self.retrieval_enabled:
            return ("RETRIEVAL DISABLED — the retrieval ablation off-arm. Not a miss: no "
                    "lookup was attempted.")
        if not self.documents_available:
            return ("NO PRIOR KNOWLEDGE — document retrieval is unavailable on this "
                    "catalog (no documents exist, so the tools are hidden). This is "
                    "an explicit miss, not a clean result.")
        if not self.documents:
            return "NO PRIOR KNOWLEDGE — document retrieval ran and matched nothing."
        return f"PREVIOUS VERIFIED INCIDENT — {len(self.documents)} document(s) retrieved."


class Archivist:
    """Role: LLM + tools; allowlist `search_documents`, `grep_documents`, tool-list.

    Runs without a model here. Retrieval and capability negotiation are both
    mechanical, and the agent design explicitly endorses not putting a model where one is not
    needed. When the Diagnostician's reasoning step runs, the retrieved
    text is what it consumes — fenced, never as instruction.
    """

    NAME = "archivist"

    def __init__(self, client: DataHubMCPClient, builder: EvidenceBuilder,
                 pack: ProofPack, sentinel: Optional[Sentinel] = None,
                 *, retrieval: bool = True) -> None:
        self._client = client
        self._builder = builder
        self._pack = pack
        self._sentinel = sentinel or Sentinel()
        # The retrieval ablation's ablation flag. When False the agent still negotiates
        # capabilities — that is step 4 and is not what is being ablated — but
        # performs no document retrieval at all. Reported distinctly from "the
        # tools were unavailable", because a disabled feature and a missing
        # capability are different facts and the Diagnostician must be able to
        # tell them apart.
        self._retrieval = retrieval

    # ------------------------------------------------------------------ step 4

    def negotiate(self) -> tuple[Capabilities, Evidence]:
        """step 4: list the tools and record the capability set as evidence."""
        caps = self._client.capabilities
        raw_ref = self._pack.write(
            "archivist/capabilities.json",
            {
                "serverInfo": {"name": caps.server_name, "version": caps.server_version},
                "protocolVersion": caps.protocol_version,
                "tool_count": len(caps.tools),
                "tools": sorted(caps.tool_names),
                "input_schemas": {n: caps.describe(n) for n in sorted(caps.tool_names)},
            },
            note="The negotiated capability set for THIS run. Not a constant.",
        )
        evidence = self._builder.make(
            source=EvidenceSource.RUNTIME,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=f"MCP server offered {len(caps.tools)} tools; "
                  f"document tools {'present' if self._docs_available(caps) else 'hidden'}",
            raw_ref=raw_ref,
        )
        return caps, evidence

    @staticmethod
    def _docs_available(caps: Capabilities) -> bool:
        return bool(DOCUMENT_TOOLS & caps.tool_names)

    # ------------------------------------------------------------------ step 8

    def retrieve(self, query: str) -> tuple[PriorKnowledge, list[Evidence], list[ToolCallRecord]]:
        caps = self._client.capabilities
        available = self._docs_available(caps)
        records: list[ToolCallRecord] = []
        evidence: list[Evidence] = []
        screened: list[ScreenedText] = []

        if not self._retrieval:
            evidence.append(self._builder.make(
                source=EvidenceSource.RUNTIME,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim="document retrieval DISABLED for this run (the retrieval ablation off-arm)",
                raw_ref=self._pack.write(
                    "archivist/retrieval-disabled.txt",
                    "the retrieval ablation: retrieval=off.\n\n"
                    "No document lookup was attempted. This is a deliberate "
                    "experimental condition, NOT a failed or empty search — the "
                    "tools were offered by the server and simply not called.\n\n"
                    f"negotiated tools ({len(caps.tools)}): {sorted(caps.tool_names)}\n",
                    note="Ablation off-arm. No lookup attempted."),
            ))
        elif available:
            # Two stages, because that is how the tools are actually designed —
            # discovered the hard way against the live server.
            #
            #   search_documents(query)      -> URNs + titles, deliberately NO
            #                                   content ("to avoid context bloat")
            #   grep_documents(urns, pattern) -> content for those specific URNs
            #
            # Calling them independently, as an earlier version did, gets a hit
            # list with no bodies and a grep that fails for want of `urns`. The
            # visible symptom was the worst possible one: DevGuard reporting
            # "NO PRIOR KNOWLEDGE" while the runbook it had written minutes
            # earlier sat in the result set.
            search = self._client.call(self.NAME, "search_documents", {"query": query})
            search_ref = self._pack.write(
                "archivist/search_documents.json",
                {"arguments": {"query": query}, "ok": search.ok,
                 "text": search.text, "error": search.error},
                note="Stage 1 — document discovery. Returns URNs and titles, not bodies.",
            )
            records.append(search.record(search_ref))

            hits = _search_hits(search.text) if search.ok else []
            if hits and caps.has("grep_documents"):
                urns = [h["urn"] for h in hits]
                grep_args = {"urns": urns, "pattern": query}
                grep = self._client.call(self.NAME, "grep_documents", grep_args)
                grep_ref = self._pack.write(
                    "archivist/grep_documents.json",
                    {"arguments": grep_args, "ok": grep.ok, "text": grep.text,
                     "error": grep.error},
                    note="Stage 2 — content for the URNs stage 1 found.",
                )
                records.append(grep.record(grep_ref))
                bodies = _grep_bodies(grep.text) if grep.ok else {}
            else:
                bodies = {}

            for hit in hits:
                body = bodies.get(hit["urn"]) or hit["title"]
                screened.append(self._sentinel.screen(
                    hit["urn"].replace(":", "_").replace("/", "_"), body))

            for doc in screened:
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_DOCUMENT,
                    # The Evidence model would reject anything else here; stated
                    # explicitly so the intent is legible at the call site too.
                    trust=EvidenceTrust.UNTRUSTED_TEXT,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=f"retrieved prior document {doc.field} "
                          f"(injection screen: {doc.risk.value})",
                    raw_ref=self._pack.write(
                        f"archivist/document-{doc.field}.txt", doc.original,
                        note="UNTRUSTED. Fenced before entering any prompt.",
                    ),
                ))
        else:
            # The deliberate degradation. Recorded as RUNTIME evidence because
            # "the tools were not offered" is an observed fact about this run,
            # and the Diagnostician must be able to see that retrieval was
            # unavailable rather than merely empty.
            evidence.append(self._builder.make(
                source=EvidenceSource.RUNTIME,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim="document retrieval unavailable — search_documents/grep_documents "
                      "are not offered by this catalog (documented server behaviour)",
                raw_ref=self._pack.write(
                    "archivist/documents-unavailable.txt",
                    "search_documents and grep_documents are absent from the negotiated "
                    "tool set.\n\n"
                    "By documented behaviour these tools are hidden automatically when "
                    "the catalog holds no documents. This is expected on incident #1 and "
                    "is reported as DEGRADED, never as a clean 'no prior incidents'.\n\n"
                    f"negotiated tools ({len(caps.tools)}): {sorted(caps.tool_names)}\n",
                    note="Explicit miss. Not an error, and not a clean result.",
                ),
            ))

        knowledge = PriorKnowledge(
            documents_available=available,
            retrieval_enabled=self._retrieval,
            documents=tuple(screened),
            negotiated_tools=caps.tool_names,
            missing_from_contract=caps.missing_from(
                AGENT_TOOL_ALLOWLISTS[self.NAME] | DOCUMENT_TOOLS
            ),
        )
        return knowledge, evidence, records

    def run(self, query: str, *, to_agent: str = "cartographer"):
        started = now()
        caps_evidence_pair = self.negotiate()
        knowledge, evidence, records = self.retrieve(query)
        all_evidence = [caps_evidence_pair[1], *evidence]

        decision = (AgentDecision.OK
                    if (knowledge.documents_available or not self._retrieval)
                    else AgentDecision.DEGRADED)
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent,
            incident_id=self._builder._incident_id,
            evidence_ids=tuple(e.id for e in all_evidence),
            decision=decision,
            rationale=knowledge.summary,
            started_at=started, ended_at=now(),
            tokens=0, model=None,
            tool_calls=tuple(records),
        )
        return knowledge, all_evidence, handoff


# --------------------------------------------------------------------- parsing
#
# Free functions so the response shapes are unit-testable without a live server,
# and pinned against real captured payloads in tests/test_archivist_retrieval.py.

def _search_hits(text: str) -> list[dict]:
    """URN + title per hit, read from `searchResults` — the real shape.

    An earlier version looked for `documents` / `results`, keys the server does
    not use, and therefore reported every successful search as empty.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    hits: list[dict] = []
    for result in (payload.get("searchResults") or []) if isinstance(payload, dict) else []:
        entity = (result or {}).get("entity") or {}
        urn = entity.get("urn")
        if not isinstance(urn, str):
            continue
        hits.append({
            "urn": urn,
            "title": ((entity.get("info") or {}).get("title") or urn),
            "sub_type": entity.get("subType"),
        })
    return hits


def _grep_bodies(text: str) -> dict[str, str]:
    """{urn: matched content} from a grep_documents response."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    bodies: dict[str, str] = {}
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            urn = node.get("urn") or node.get("documentUrn")
            matches = node.get("matches") or node.get("snippets") or node.get("content")
            if isinstance(urn, str) and matches is not None:
                if isinstance(matches, list):
                    rendered = "\n".join(
                        m.get("text", json.dumps(m)) if isinstance(m, dict) else str(m)
                        for m in matches)
                else:
                    rendered = str(matches)
                bodies.setdefault(urn, rendered)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return bodies
