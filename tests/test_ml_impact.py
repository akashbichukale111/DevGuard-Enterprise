"""
The ML model at the end of the blast radius, read rather than merely counted.

The Pathfinder already proved a renamed column reaches a registered `mlModel`.
Until now that was the whole finding: a URN. Nothing about which model, what
version, or — the operationally load-bearing part — whether a human is on the
hook for it. The model's owner is very often not the dataset's owner, so
routing the incident to the dataset owner alone leaves the person whose model
is now training on a moved column completely unaware.

These tests pin the extraction against DataHub's real `MLModel` shape, and pin
the failure modes that would quietly shrink a blast radius.
"""

from __future__ import annotations

import json

import pytest

from backend.v2.evidence import EvidenceBuilder, EvidenceConfidence, EvidenceSource
from backend.v2.ml_impact import (
    MAX_MODELS,
    MLImpactReader,
    MLModelImpact,
    _models_from_text,
)

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn_predictor,PROD)"
MODEL_2 = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,ltv_model,PROD)"
OWNER = "urn:li:corpuser:data_science"


class _Result:
    """Mirrors DataHubMCPClient's ToolResult, including `record()`."""

    def __init__(self, text: str = "", ok: bool = True, error=None,
                 tool: str = "get_entities", arguments=None):
        self.text, self.ok, self.error = text, ok, error
        self.tool, self.arguments, self.duration_ms = tool, arguments or {}, 0.0

    def record(self, raw_ref=None):
        from backend.v2.handoff import ToolCallRecord

        return ToolCallRecord(tool=self.tool, arguments=self.arguments, ok=self.ok,
                              duration_ms=self.duration_ms, raw_ref=raw_ref,
                              error=self.error)


class _Client:
    """Records the call and returns a canned response."""

    def __init__(self, payload, ok: bool = True):
        self._text = payload if isinstance(payload, str) else json.dumps(payload)
        self._ok = ok
        self.calls: list[tuple] = []

    def call(self, agent, tool, args):
        self.calls.append((agent, tool, args))
        return _Result(self._text, ok=self._ok, tool=tool, arguments=args)


def _entity(urn: str, **over) -> dict:
    """A response shaped like DataHub's real MLModel type."""
    base = {
        "urn": urn,
        "type": "MLMODEL",
        "name": "churn_predictor",
        "description": "Predicts 30-day churn.",
        "platform": {"name": "mlflow"},
        "origin": "PROD",
        "ownership": {"owners": [{"owner": {"urn": OWNER, "type": "CORP_USER"}}]},
        "properties": {"version": "3.2.1",
                       "trainingMetrics": [{"name": "auc", "value": "0.91"}]},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Extraction against the real shape
# --------------------------------------------------------------------------- #

def test_the_real_mlmodel_shape_is_parsed():
    models = _models_from_text(json.dumps({"entities": [_entity(MODEL)]}))
    m = models[MODEL]
    assert m.name == "churn_predictor"
    assert m.version == "3.2.1"
    assert m.platform == "mlflow"
    assert m.origin == "PROD"
    assert m.owners == (OWNER,)
    assert m.training_metrics == ("auc",)
    assert m.enriched


def test_nesting_depth_does_not_matter():
    """
    Shape tolerance is the point: `get_entities` nests differently by server
    version and by whether an entity has siblings. A brittle path would fail
    only against a live catalog.
    """
    deep = {"data": {"result": {"batch": [{"entity": _entity(MODEL)}]}}}
    assert MODEL in _models_from_text(json.dumps(deep))


def test_only_ml_models_are_extracted():
    """A dataset in the same response must not be reported as a model."""
    payload = {"entities": [
        _entity(MODEL),
        {"urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,a.b,PROD)", "name": "b"},
    ]}
    assert list(_models_from_text(json.dumps(payload))) == [MODEL]


def test_group_ownership_counts_as_an_owner():
    e = _entity(MODEL, ownership={"owners": [
        {"owner": {"urn": "urn:li:corpGroup:ml-platform"}}]})
    assert _models_from_text(json.dumps({"entities": [e]}))[MODEL].owners == (
        "urn:li:corpGroup:ml-platform",)


def test_a_model_with_no_ownership_has_no_owners():
    e = _entity(MODEL, ownership=None)
    m = _models_from_text(json.dumps({"entities": [e]}))[MODEL]
    assert m.owners == ()
    assert not m.has_owner


def test_properties_supply_name_when_the_top_level_lacks_one():
    e = _entity(MODEL, name=None)
    e["properties"]["name"] = "from_properties"
    assert _models_from_text(json.dumps({"entities": [e]}))[MODEL].name == "from_properties"


def test_malformed_text_yields_nothing_rather_than_raising():
    assert _models_from_text("not json") == {}
    assert _models_from_text("") == {}


def test_display_falls_back_to_the_urn_when_unnamed():
    m = MLModelImpact(urn=MODEL)
    assert m.display == "churn_predictor"


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #

def test_the_reader_calls_get_entities_with_the_discovered_urns():
    c = _Client({"entities": [_entity(MODEL)]})
    MLImpactReader(c).read([MODEL])
    agent, tool, args = c.calls[0]
    assert tool == "get_entities"
    assert args["urns"] == [MODEL]


def test_no_models_means_no_call_at_all():
    """A blast radius reaching no model must not spend a catalog round trip."""
    c = _Client({})
    report = MLImpactReader(c).read([])
    assert c.calls == []
    assert report.reached == 0
    assert "reaches no registered ML model" in report.summary()


def test_a_urn_the_response_omits_is_still_reported():
    """
    The load-bearing failure mode. Lineage proved the model is affected; if the
    detail read comes back without it, the model must NOT vanish from the blast
    radius — it degrades to "known but unenriched".
    """
    report = MLImpactReader(_Client({"entities": []})).read([MODEL])
    assert report.reached == 1
    assert report.models[0].urn == MODEL
    assert report.models[0].enriched is False


def test_a_failed_call_still_preserves_the_blast_radius():
    report = MLImpactReader(_Client("garbage", ok=False)).read([MODEL, MODEL_2])
    assert report.reached == 2
    assert all(not m.enriched for m in report.models)


def test_duplicate_urns_are_collapsed():
    c = _Client({"entities": [_entity(MODEL)]})
    report = MLImpactReader(c).read([MODEL, MODEL, MODEL])
    assert report.reached == 1
    assert c.calls[0][2]["urns"] == [MODEL]


def test_reading_is_capped_and_truncation_is_visible():
    urns = [f"urn:li:mlModel:(urn:li:dataPlatform:mlflow,m{i},PROD)"
            for i in range(MAX_MODELS + 5)]
    report = MLImpactReader(_Client({"entities": []})).read(urns)
    assert report.truncated
    assert report.queried == MAX_MODELS


def test_lineage_order_is_preserved():
    c = _Client({"entities": [_entity(MODEL_2, name="ltv"), _entity(MODEL)]})
    report = MLImpactReader(c).read([MODEL, MODEL_2])
    assert [m.urn for m in report.models] == [MODEL, MODEL_2]


# --------------------------------------------------------------------------- #
# Unowned models — the finding an operator actually needs
# --------------------------------------------------------------------------- #

def test_unowned_models_are_identified():
    c = _Client({"entities": [
        _entity(MODEL),
        _entity(MODEL_2, name="ltv_model", ownership=None),
    ]})
    report = MLImpactReader(c).read([MODEL, MODEL_2])
    assert [m.urn for m in report.unowned] == [MODEL_2]
    assert report.notifiable_actors == (OWNER,)


def test_an_unenriched_model_is_not_counted_as_unowned():
    """
    "We could not read it" and "it has no owner" are different claims, and
    conflating them would manufacture an alarming finding out of a failed read.
    """
    report = MLImpactReader(_Client({"entities": []})).read([MODEL])
    assert report.unowned == ()


def test_the_summary_counts_owned_and_unowned():
    c = _Client({"entities": [_entity(MODEL), _entity(MODEL_2, ownership=None)]})
    s = MLImpactReader(c).read([MODEL, MODEL_2]).summary()
    assert "2 ML model(s)" in s and "1 have a registered owner" in s


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

def test_each_model_becomes_citable_evidence():
    c = _Client({"entities": [_entity(MODEL)]})
    reader = MLImpactReader(c)
    report = reader.read([MODEL])
    items = reader.evidence(EvidenceBuilder("INC-1"), report, "ref.json")
    assert len(items) == 1
    assert items[0].source is EvidenceSource.DATAHUB_GRAPH
    assert items[0].datahub_urn == MODEL
    assert "churn_predictor" in items[0].claim


def test_an_unenriched_model_carries_weaker_confidence():
    reader = MLImpactReader(_Client({"entities": []}))
    report = reader.read([MODEL])
    items = reader.evidence(EvidenceBuilder("INC-1"), report, "ref.json")
    assert items[0].confidence is EvidenceConfidence.INFERRED


def test_an_enriched_model_is_observed():
    reader = MLImpactReader(_Client({"entities": [_entity(MODEL)]}))
    report = reader.read([MODEL])
    items = reader.evidence(EvidenceBuilder("INC-1"), report, "ref.json")
    assert items[0].confidence is EvidenceConfidence.OBSERVED


def test_unowned_models_get_their_own_citable_finding():
    """
    "This model has nobody to tell" is an operational finding a reviewer should
    be able to click on, not a footnote inside another claim.
    """
    reader = MLImpactReader(_Client({"entities": [_entity(MODEL, ownership=None)]}))
    report = reader.read([MODEL])
    items = reader.evidence(EvidenceBuilder("INC-1"), report, "ref.json")
    assert len(items) == 2
    assert "no registered owner" in items[-1].claim


def test_no_unowned_finding_when_everything_is_owned():
    reader = MLImpactReader(_Client({"entities": [_entity(MODEL)]}))
    report = reader.read([MODEL])
    items = reader.evidence(EvidenceBuilder("INC-1"), report, "ref.json")
    assert len(items) == 1


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #

def test_the_reader_never_writes():
    """
    ML entities are outside the mutation allowlist's two entity types on
    purpose. Widening it to reach them is a governance decision, not a
    convenience this module may take.
    """
    from backend.v2.mcp_contract import WRITING_TOOLS

    c = _Client({"entities": [_entity(MODEL)]})
    MLImpactReader(c).read([MODEL])
    assert all(tool not in WRITING_TOOLS for _, tool, _ in c.calls)


def test_the_reader_runs_under_an_agent_granted_get_entities():
    from backend.v2.handoff import AGENT_TOOL_ALLOWLISTS

    assert "get_entities" in AGENT_TOOL_ALLOWLISTS[MLImpactReader.NAME]


# --------------------------------------------------------------------------- #
# The wiring — without this the module above is a library nobody calls
# --------------------------------------------------------------------------- #

def _pathfinder(tmp_path, ml_reader):
    from backend.v2.agents.pathfinder import Pathfinder
    from backend.v2.evidence import EvidenceBuilder
    from backend.v2.proofpack import ProofPack

    # The real `get_lineage` envelope: impacted entities live under
    # downstreams.searchResults, each with a `degree`. `_impacted()`
    # deliberately reads only from there — walking the whole payload once swept
    # DataHub's facet aggregations in as impacted assets.
    lineage = {"downstreams": {"searchResults": [
        {"degree": 2, "entity": {"urn": MODEL, "type": "MLMODEL"}},
        {"degree": 1, "entity": {
            "urn": "urn:li:dataJob:(urn:li:dataFlow:(dbt,p,PROD),j)",
            "type": "DATA_JOB"}},
    ]}}
    client = _Client(lineage)
    return Pathfinder(client, EvidenceBuilder("INC-1"), ProofPack(tmp_path, "r"),
                      ml_reader=ml_reader), client


def test_the_pathfinder_enriches_the_models_it_finds(tmp_path):
    """Proven at the agent boundary, not by inspection."""
    reader = MLImpactReader(_Client({"entities": [_entity(MODEL)]}))
    pf, _ = _pathfinder(tmp_path, reader)
    _, evidence, _ = pf.run("urn:li:dataset:(urn:li:dataPlatform:postgres,a.b,PROD)")

    claims = " ".join(e.claim for e in evidence)
    assert "churn_predictor" in claims, "the model was found but never enriched"
    assert OWNER in claims, "the model's owner never reached the evidence chain"


def test_without_a_reader_the_pathfinder_behaves_exactly_as_before(tmp_path):
    """
    Backwards compatibility, deliberately: every replay bundle recorded before
    this existed must keep deserialising unchanged.
    """
    pf, _ = _pathfinder(tmp_path, None)
    _, evidence, _ = pf.run("urn:li:dataset:(urn:li:dataPlatform:postgres,a.b,PROD)")

    claims = " ".join(e.claim for e in evidence)
    assert "terminates at registered ML model" in claims
    # The URN itself contains the model's name, so absence of enrichment has to
    # be asserted on something only enrichment produces.
    assert OWNER not in claims
    assert "owner:" not in claims


def test_the_bare_urn_finding_survives_enrichment(tmp_path):
    """Enrichment adds; it must not replace the lineage finding it builds on."""
    reader = MLImpactReader(_Client({"entities": [_entity(MODEL)]}))
    pf, _ = _pathfinder(tmp_path, reader)
    _, evidence, _ = pf.run("urn:li:dataset:(urn:li:dataPlatform:postgres,a.b,PROD)")

    claims = " ".join(e.claim for e in evidence)
    assert "terminates at registered ML model" in claims
