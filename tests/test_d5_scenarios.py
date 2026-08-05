"""
Pins the two live diagnosis outcomes against their committed proof packs.

`scripts/run_d5_diagnosis.py` needs a live substrate and a live DataHub, so it
cannot run in CI. What CI *can* do is assert that the committed evidence still
says what the write-up claims it says — which is the failure mode that actually
matters. Documentation drifting away from its own evidence is how a project ends
up claiming something its artifacts do not support.

So: if someone edits the packs, or regenerates them into a different shape,
these fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PACKS = Path("evidence/proof-pack")
REFUSAL = PACKS / "d5-refusal"
FULL = PACKS / "d5-full"


def load(pack: Path, name: str) -> dict:
    return json.loads((pack / name).read_text())


pytestmark = pytest.mark.skipif(
    not (REFUSAL / "diagnosis.json").exists(),
    reason="D5 proof packs not present in this checkout",
)


class TestRefusalScenario:
    """The refusal requirement: 'refusal demonstrated'."""

    def test_chain_is_genuinely_insufficient(self):
        chain = load(REFUSAL, "evidence-chain.json")
        assert chain["is_sufficient"] is False
        assert chain["missing_for_sufficiency"] == ["DATAHUB_GRAPH"]

    def test_runtime_evidence_is_real_and_present(self):
        """The refusal must be about missing graph evidence, not an empty run."""
        chain = load(REFUSAL, "evidence-chain.json")
        sources = {i["source"] for i in chain["items"]}
        assert sources == {"RUNTIME"}
        assert len(chain["items"]) >= 3

    def test_the_catalog_was_actually_unreachable(self):
        """Not a contrived gap — the tool calls really failed."""
        assert load(REFUSAL, "evidence-chain.json")["datahub_error"]
        assert (REFUSAL / "datahub-unavailable.txt").read_text().strip()

    def test_verdict_is_a_reasoned_refusal(self):
        d = load(REFUSAL, "diagnosis.json")
        assert d["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert d["is_refusal"] is True
        assert d["refusal_reasons"] == ["NO_GRAPH_EVIDENCE"]

    def test_no_model_was_consulted(self):
        """Refusal is pre-model: no tokens, no attribution, no network."""
        d = load(REFUSAL, "diagnosis.json")
        assert d["model"] is None
        assert d["tokens"] == 0

    def test_no_root_cause_was_claimed(self):
        assert load(REFUSAL, "diagnosis.json")["cited_evidence_ids"] == []


class TestFullScenario:
    def test_chain_is_sufficient(self):
        chain = load(FULL, "evidence-chain.json")
        assert chain["is_sufficient"] is True
        assert {i["source"] for i in chain["items"]} == {"RUNTIME", "DATAHUB_GRAPH"}
        assert chain["datahub_error"] is None

    def test_blocked_is_not_reported_as_a_refusal(self):
        """The distinction the whole D5 gate depends on.

        The chain here is sufficient, so a refusal would be a false claim of
        judgement. `api.groq.com` is unreachable, and that is what the evidence
        says.
        """
        d = load(FULL, "diagnosis.json")
        assert d["verdict"] == "REASONER_UNAVAILABLE"
        assert d["is_refusal"] is False

    def test_no_root_cause_is_claimed_without_a_model(self):
        d = load(FULL, "diagnosis.json")
        assert d["cited_evidence_ids"] == []
        assert d["tokens"] == 0

    def test_blast_radius_still_reaches_the_ml_model(self):
        chain = load(FULL, "evidence-chain.json")
        claims = " ".join(i["claim"] for i in chain["items"])
        assert "terminates at registered ML model" in claims
        assert "urn:li:mlModel:" in claims


class TestTheTwoScenariosDiffer:
    def test_only_the_refusal_run_is_a_refusal(self):
        assert load(REFUSAL, "diagnosis.json")["is_refusal"] is True
        assert load(FULL, "diagnosis.json")["is_refusal"] is False

    def test_verdicts_are_distinct(self):
        assert (load(REFUSAL, "diagnosis.json")["verdict"]
                != load(FULL, "diagnosis.json")["verdict"])

    def test_neither_run_fabricated_a_root_cause(self):
        for pack in (REFUSAL, FULL):
            d = load(pack, "diagnosis.json")
            assert d["verdict"] != "ROOT_CAUSE_IDENTIFIED"
            assert d["cited_evidence_ids"] == []


class TestPacksAreClean:
    @pytest.mark.parametrize("pack", [REFUSAL, FULL])
    def test_no_credentials_in_the_committed_evidence(self, pack):
        import re
        leak = re.compile(r"gsk_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
        for path in pack.rglob("*"):
            if path.is_file():
                assert not leak.search(path.read_text(errors="ignore")), path

    @pytest.mark.parametrize("pack", [REFUSAL, FULL])
    def test_index_agrees_with_the_diagnosis(self, pack):
        index = load(pack, "INDEX.json")
        diagnosis = load(pack, "diagnosis.json")
        assert index["diagnostician_verdict"] == diagnosis["verdict"]
        assert index["diagnostician_is_refusal"] == diagnosis["is_refusal"]
