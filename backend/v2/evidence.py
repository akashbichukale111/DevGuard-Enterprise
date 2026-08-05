"""
evidence.py — the evidence rule Evidence & Provenance contract, typed.

The evidence rule opens with "Typed contracts everywhere. No raw dicts across module
boundaries." That is not decoration here: the whole claim DevGuard makes is that
its conclusions are *traceable to something real*, and the only way that claim
survives contact with a judge is if provenance is a field the type system
enforces rather than a convention people remember.

Three axes, deliberately independent:

  source      WHERE it came from   (a runtime failure? the catalog? our own head?)
  trust       whether an attacker could have written it
  confidence  whether we watched it happen or reasoned our way to it

They are independent because they genuinely vary independently. A dataset
description read from DataHub is `DATAHUB_GRAPH` + `UNTRUSTED_TEXT` + `OBSERVED`:
we really did observe it, and it is really attacker-controllable. A root cause is
`DEVGUARD_INFERENCE` + `TRUSTED_SYSTEM` + `INFERRED`. Collapsing these into one
"confidence" number is exactly the mush the evidence rule is written to prevent.

The rule that matters most is `EvidenceChain.is_sufficient`: a root cause needs
at least one RUNTIME and at least one DATAHUB_GRAPH item. The evidence rule: "If the chain
cannot form, the Diagnostician returns INSUFFICIENT_EVIDENCE and the loop stops.
An agent that knows when to stop scores higher than one that always answers."
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceSource(str, Enum):
    """Where a piece of evidence physically came from (evidence rule)."""

    RUNTIME = "RUNTIME"
    """Observed from a real process: an exit code, a traceback, a query result."""

    DATAHUB_GRAPH = "DATAHUB_GRAPH"
    """Read from the DataHub metadata graph — lineage, schema, entities."""

    DATAHUB_DOCUMENT = "DATAHUB_DOCUMENT"
    """Read from a DataHub Context Document. Human-authored, so untrusted."""

    REPO_STATIC = "REPO_STATIC"
    """Read from this repository without executing anything."""

    DEVGUARD_INFERENCE = "DEVGUARD_INFERENCE"
    """DevGuard's own conclusion. The evidence rule requires this to render visually distinct."""

    SEEDED_DEMO = "SEEDED_DEMO"
    """Fabricated for a demo. The evidence rule: "SEEDED_DEMO is loud and unmissable.\""""


class EvidenceTrust(str, Enum):
    """Whether the content could have been written by an attacker (the untrusted-content boundary)."""

    TRUSTED_SYSTEM = "TRUSTED_SYSTEM"
    """Produced by a system we control. Not attacker-authored."""

    UNTRUSTED_TEXT = "UNTRUSTED_TEXT"
    """Free text from a shared catalog. Anyone with write access authored it."""


class EvidenceConfidence(str, Enum):
    """How the claim was arrived at (evidence rule)."""

    OBSERVED = "OBSERVED"
    """We saw it. A command ran and this is its output."""

    DERIVED = "DERIVED"
    """Mechanically computed from something observed, without judgement."""

    INFERRED = "INFERRED"
    """A judgement call. The evidence rule requires this to render visually distinct."""


#: Sources whose content is attacker-controllable, and therefore may never be
#: emitted as TRUSTED_SYSTEM. Catalog free-text is the untrusted-content boundary injection vector.
_INHERENTLY_UNTRUSTED = {
    EvidenceSource.DATAHUB_DOCUMENT,
}


class Evidence(BaseModel):
    """One atomic, attributable fact (evidence rule).

    `raw_ref` is mandatory and is the whole point: every claim points at a file
    in the proof pack that a judge can open. A claim with no raw_ref is prose,
    and the evidence rule exists to keep prose out of the evidence chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., description="EV-<incident>-<seq>")
    source: EvidenceSource
    trust: EvidenceTrust
    confidence: EvidenceConfidence
    collected_at: datetime
    claim: str = Field(..., min_length=1, description="One-line assertion.")
    raw_ref: str = Field(..., min_length=1, description="Path into the proof pack.")
    datahub_urn: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        # Enforced rather than documented, because these ids are what the UI
        # renders as clickable chips — a malformed one is a dead link.
        parts = v.split("-")
        if len(parts) < 3 or parts[0] != "EV":
            raise ValueError(f"evidence id must look like EV-<incident>-<seq>, got {v!r}")
        return v

    @field_validator("collected_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("collected_at must be timezone-aware (the evidence rule says UTC)")
        return v.astimezone(timezone.utc)

    @field_validator("claim")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("claim is a ONE-line assertion; put detail in raw_ref")
        return v

    def model_post_init(self, __context) -> None:
        if self.source in _INHERENTLY_UNTRUSTED and self.trust is not EvidenceTrust.UNTRUSTED_TEXT:
            raise ValueError(
                f"{self.source.value} is attacker-authorable and must be UNTRUSTED_TEXT"
            )

    @property
    def is_untrusted(self) -> bool:
        return self.trust is EvidenceTrust.UNTRUSTED_TEXT

    @property
    def needs_visual_distinction(self) -> bool:
        """The evidence rule: DEVGUARD_INFERENCE / INFERRED / SEEDED_DEMO render differently."""
        return (
            self.source in (EvidenceSource.DEVGUARD_INFERENCE, EvidenceSource.SEEDED_DEMO)
            or self.confidence is EvidenceConfidence.INFERRED
        )


class EvidenceChain(BaseModel):
    """An ordered chain, and the evidence rule that decides whether it is enough."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    items: tuple[Evidence, ...] = ()

    @property
    def sources(self) -> set[EvidenceSource]:
        return {e.source for e in self.items}

    @property
    def is_sufficient(self) -> bool:
        """The evidence rule: root cause needs >=1 RUNTIME and >=1 DATAHUB_GRAPH.

        Note what this does NOT do: it does not count items, and it does not
        accept a pile of DATAHUB_GRAPH facts as a substitute for having watched
        something fail. Catalog metadata alone can describe a system that has
        been broken for a month; it cannot establish that anything happened.
        """
        return (
            EvidenceSource.RUNTIME in self.sources
            and EvidenceSource.DATAHUB_GRAPH in self.sources
        )

    @property
    def missing_for_sufficiency(self) -> tuple[EvidenceSource, ...]:
        """Exactly what a refusal message should name."""
        return tuple(
            s for s in (EvidenceSource.RUNTIME, EvidenceSource.DATAHUB_GRAPH)
            if s not in self.sources
        )

    @property
    def untrusted_items(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.items if e.is_untrusted)

    def add(self, *evidence: Evidence) -> "EvidenceChain":
        """Return a new chain. Immutability keeps handoffs auditable."""
        seen = {e.id for e in self.items}
        for e in evidence:
            if e.id in seen:
                raise ValueError(f"duplicate evidence id {e.id}")
            seen.add(e.id)
        return EvidenceChain(incident_id=self.incident_id, items=self.items + evidence)

    def by_id(self, evidence_id: str) -> Evidence:
        for e in self.items:
            if e.id == evidence_id:
                return e
        raise KeyError(evidence_id)

    def digest(self) -> str:
        """Stable hash of the chain's content.

        The write-back rules requires every DataHub write to embed the evidence chain that
        justified it, and the generated-numbers rule requires the write-back to be idempotent. A
        content hash gives both: the same chain produces the same digest, so a
        re-run can recognise its own previous write instead of duplicating it.
        """
        payload = json.dumps(
            [
                {
                    "id": e.id, "source": e.source.value, "trust": e.trust.value,
                    "confidence": e.confidence.value, "claim": e.claim,
                    "raw_ref": e.raw_ref, "datahub_urn": e.datahub_urn,
                }
                for e in self.items
            ],
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class EvidenceBuilder:
    """Allocates sequential, well-formed evidence ids for one incident.

    Exists so no agent ever formats an id by hand — the evidence rule's id shape is load-bearing
    for the UI, and hand-formatting is where it would drift.
    """

    def __init__(self, incident_id: str, *, clock=None) -> None:
        self._incident_id = incident_id
        self._seq = 0
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def make(
        self,
        *,
        source: EvidenceSource,
        trust: EvidenceTrust,
        confidence: EvidenceConfidence,
        claim: str,
        raw_ref: str,
        datahub_urn: Optional[str] = None,
    ) -> Evidence:
        self._seq += 1
        return Evidence(
            id=f"EV-{self._incident_id}-{self._seq:03d}",
            source=source,
            trust=trust,
            confidence=confidence,
            collected_at=self._clock(),
            claim=claim,
            raw_ref=raw_ref,
            datahub_urn=datahub_urn,
        )
