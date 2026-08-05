"""
Tests for the evidence rule Evidence & Provenance contract.

The rule these exist to pin is the sufficiency rule. The evidence rule says a root cause needs
>=1 RUNTIME and >=1 DATAHUB_GRAPH, and that "an agent that knows when to stop
scores higher than one that always answers". A regression that quietly widened
`is_sufficient` would not fail anything else in the suite — the loop would just
start answering questions it cannot support, which is the exact failure mode the
contract is written to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceChain, EvidenceConfidence, EvidenceSource,
    EvidenceTrust,
)

UTC_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def make(source: EvidenceSource, *, seq: int = 1,
         trust: EvidenceTrust = EvidenceTrust.TRUSTED_SYSTEM,
         confidence: EvidenceConfidence = EvidenceConfidence.OBSERVED) -> Evidence:
    return Evidence(
        id=f"EV-INC1-{seq:03d}", source=source, trust=trust, confidence=confidence,
        collected_at=UTC_NOW, claim="a claim", raw_ref="evidence/x.json",
    )


class TestSufficiency:
    def test_runtime_plus_graph_is_sufficient(self):
        chain = EvidenceChain(incident_id="INC1").add(
            make(EvidenceSource.RUNTIME, seq=1),
            make(EvidenceSource.DATAHUB_GRAPH, seq=2),
        )
        assert chain.is_sufficient
        assert chain.missing_for_sufficiency == ()

    def test_runtime_alone_is_not_sufficient(self):
        chain = EvidenceChain(incident_id="INC1").add(make(EvidenceSource.RUNTIME))
        assert not chain.is_sufficient
        assert chain.missing_for_sufficiency == (EvidenceSource.DATAHUB_GRAPH,)

    def test_graph_alone_is_not_sufficient(self):
        chain = EvidenceChain(incident_id="INC1").add(make(EvidenceSource.DATAHUB_GRAPH))
        assert not chain.is_sufficient
        assert chain.missing_for_sufficiency == (EvidenceSource.RUNTIME,)

    def test_a_pile_of_graph_evidence_still_is_not_sufficient(self):
        """Ten catalog facts do not establish that anything happened.

        This is the case worth pinning: catalog metadata can describe a system
        that has been broken for a month without anything observing a failure.
        """
        chain = EvidenceChain(incident_id="INC1").add(
            *[make(EvidenceSource.DATAHUB_GRAPH, seq=i) for i in range(1, 11)]
        )
        assert not chain.is_sufficient

    def test_inference_does_not_substitute_for_observation(self):
        chain = EvidenceChain(incident_id="INC1").add(
            make(EvidenceSource.DEVGUARD_INFERENCE, seq=1),
            make(EvidenceSource.DATAHUB_GRAPH, seq=2),
        )
        assert not chain.is_sufficient

    def test_seeded_demo_does_not_substitute_for_runtime(self):
        """The no-fabrication rule: seeded data can never stand in for an observation."""
        chain = EvidenceChain(incident_id="INC1").add(
            make(EvidenceSource.SEEDED_DEMO, seq=1),
            make(EvidenceSource.DATAHUB_GRAPH, seq=2),
        )
        assert not chain.is_sufficient


class TestTrustInvariants:
    def test_document_evidence_cannot_claim_trusted(self):
        """Catalog documents are attacker-authorable, always."""
        with pytest.raises(ValueError, match="UNTRUSTED_TEXT"):
            make(EvidenceSource.DATAHUB_DOCUMENT, trust=EvidenceTrust.TRUSTED_SYSTEM)

    def test_document_evidence_as_untrusted_is_fine(self):
        e = make(EvidenceSource.DATAHUB_DOCUMENT, trust=EvidenceTrust.UNTRUSTED_TEXT)
        assert e.is_untrusted

    def test_untrusted_items_are_enumerable(self):
        chain = EvidenceChain(incident_id="INC1").add(
            make(EvidenceSource.RUNTIME, seq=1),
            make(EvidenceSource.DATAHUB_DOCUMENT, seq=2,
                 trust=EvidenceTrust.UNTRUSTED_TEXT),
        )
        assert len(chain.untrusted_items) == 1
        assert chain.untrusted_items[0].id == "EV-INC1-002"


class TestVisualDistinction:
    @pytest.mark.parametrize("source,confidence,expected", [
        (EvidenceSource.RUNTIME, EvidenceConfidence.OBSERVED, False),
        (EvidenceSource.DATAHUB_GRAPH, EvidenceConfidence.DERIVED, False),
        (EvidenceSource.DATAHUB_GRAPH, EvidenceConfidence.INFERRED, True),
        (EvidenceSource.DEVGUARD_INFERENCE, EvidenceConfidence.OBSERVED, True),
        (EvidenceSource.SEEDED_DEMO, EvidenceConfidence.OBSERVED, True),
    ])
    def test_flagging(self, source, confidence, expected):
        assert make(source, confidence=confidence).needs_visual_distinction is expected


class TestWellFormedness:
    def test_id_shape_is_enforced(self):
        with pytest.raises(ValueError, match="EV-<incident>-<seq>"):
            Evidence(id="nope", source=EvidenceSource.RUNTIME,
                     trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.OBSERVED,
                     collected_at=UTC_NOW, claim="c", raw_ref="r")

    def test_naive_timestamps_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            Evidence(id="EV-A-001", source=EvidenceSource.RUNTIME,
                     trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.OBSERVED,
                     collected_at=datetime(2026, 8, 1), claim="c", raw_ref="r")

    def test_claim_must_be_one_line(self):
        with pytest.raises(ValueError, match="ONE-line"):
            Evidence(id="EV-A-001", source=EvidenceSource.RUNTIME,
                     trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.OBSERVED,
                     collected_at=UTC_NOW, claim="line one\nline two", raw_ref="r")

    def test_raw_ref_is_mandatory(self):
        """A claim with no pointer to raw output is prose, not evidence."""
        with pytest.raises(ValueError):
            Evidence(id="EV-A-001", source=EvidenceSource.RUNTIME,
                     trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.OBSERVED,
                     collected_at=UTC_NOW, claim="c", raw_ref="")

    def test_duplicate_ids_rejected(self):
        chain = EvidenceChain(incident_id="INC1").add(make(EvidenceSource.RUNTIME))
        with pytest.raises(ValueError, match="duplicate"):
            chain.add(make(EvidenceSource.RUNTIME))

    def test_chain_add_is_immutable(self):
        original = EvidenceChain(incident_id="INC1").add(make(EvidenceSource.RUNTIME))
        extended = original.add(make(EvidenceSource.DATAHUB_GRAPH, seq=2))
        assert len(original.items) == 1
        assert len(extended.items) == 2


class TestDigest:
    def test_same_content_same_digest(self):
        a = EvidenceChain(incident_id="I").add(make(EvidenceSource.RUNTIME))
        b = EvidenceChain(incident_id="I").add(make(EvidenceSource.RUNTIME))
        assert a.digest() == b.digest()

    def test_different_content_different_digest(self):
        a = EvidenceChain(incident_id="I").add(make(EvidenceSource.RUNTIME))
        b = a.add(make(EvidenceSource.DATAHUB_GRAPH, seq=2))
        assert a.digest() != b.digest()

    def test_digest_ignores_collection_time(self):
        """Idempotent write-back must not be defeated by a clock tick."""
        base = dict(source=EvidenceSource.RUNTIME, trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED, claim="c", raw_ref="r")
        a = EvidenceChain(incident_id="I").add(
            Evidence(id="EV-I-001", collected_at=UTC_NOW, **base))
        b = EvidenceChain(incident_id="I").add(
            Evidence(id="EV-I-001",
                     collected_at=datetime(2027, 1, 1, tzinfo=timezone.utc), **base))
        assert a.digest() == b.digest()


class TestBuilder:
    def test_sequential_ids(self):
        builder = EvidenceBuilder("INC9", clock=lambda: UTC_NOW)
        ids = [builder.make(source=EvidenceSource.RUNTIME,
                            trust=EvidenceTrust.TRUSTED_SYSTEM,
                            confidence=EvidenceConfidence.OBSERVED,
                            claim="c", raw_ref="r").id for _ in range(3)]
        assert ids == ["EV-INC9-001", "EV-INC9-002", "EV-INC9-003"]
