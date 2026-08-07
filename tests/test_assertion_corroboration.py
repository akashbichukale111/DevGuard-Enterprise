"""
DataHub's assertions as an independent second opinion on recovery.

The Referee declares recovery by re-running the command that failed. That is
one source, and it is the same system that proposed the fix — DevGuard grading
its own homework. The catalog already holds an independent judge of dataset
health: the assertions attached to it, which in a dbt substrate are the
ingested dbt tests, authored by the data team and evaluated by the platform.

These tests pin the rule that both sources must agree, and — more importantly —
pin the ways a second opinion can be quietly turned into a rubber stamp.
"""

from __future__ import annotations

import pytest

from backend.v2.assertions import (
    ASSERTIONS_QUERY,
    AssertionCorroborator,
    Corroboration,
    recovery_is_agreed,
)
from backend.v2.evidence import EvidenceBuilder, EvidenceConfidence, EvidenceSource

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"


def _assertion(name: str, result: str | None, *, total_runs: int = 1) -> dict:
    events = (
        [{"timestampMillis": 1_700_000_000_000, "runId": "run-1",
          "status": "COMPLETE", "result": {"type": result}}]
        if result is not None else []
    )
    return {
        "urn": f"urn:li:assertion:{name}",
        "info": {"type": "DATASET", "description": name},
        "runEvents": {"total": total_runs, "failed": 0, "succeeded": 0,
                      "errored": 0, "runEvents": events},
    }


def _gql_for(assertions: list[dict], *, total: int | None = None):
    def _gql(query: str, variables: dict) -> dict:
        return {"data": {"dataset": {
            "urn": variables["urn"],
            "assertions": {"start": 0, "count": len(assertions),
                           "total": len(assertions) if total is None else total,
                           "assertions": assertions},
        }}}
    return _gql


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def test_all_passing_assertions_corroborate():
    r = AssertionCorroborator(_gql_for([
        _assertion("not_null_user_id", "SUCCESS"),
        _assertion("unique_user_id", "SUCCESS"),
    ])).corroborate(URN)
    assert r.verdict is Corroboration.CORROBORATED
    assert r.corroborated


def test_one_failing_assertion_contradicts():
    r = AssertionCorroborator(_gql_for([
        _assertion("not_null_user_id", "SUCCESS"),
        _assertion("unique_user_id", "FAILURE"),
    ])).corroborate(URN)
    assert r.verdict is Corroboration.CONTRADICTED
    assert [o.description for o in r.failing] == ["unique_user_id"]


def test_a_dataset_with_no_assertions_is_not_corroborated():
    """
    The flattering mistake, refused. "Nothing failed" is not "everything
    passed" — treating it as such silently upgrades every asset with no
    data-quality coverage into a verified one.
    """
    r = AssertionCorroborator(_gql_for([])).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED
    assert r.verdict is not Corroboration.CORROBORATED
    assert "absence of a failure is not a pass" in r.reason


def test_an_assertion_that_never_ran_cannot_vouch():
    """A partial answer must not be reported as a whole one."""
    r = AssertionCorroborator(_gql_for([
        _assertion("not_null_user_id", "SUCCESS"),
        _assertion("never_executed", None, total_runs=0),
    ])).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED
    assert len(r.without_history) == 1


def test_an_init_result_is_not_a_success():
    """INIT means started, not passed."""
    r = AssertionCorroborator(_gql_for([_assertion("in_flight", "INIT")])).corroborate(URN)
    assert r.verdict is Corroboration.CONTRADICTED


def test_only_the_most_recent_run_decides():
    """A historic failure that has since been fixed must not block recovery."""
    a = _assertion("flaky", "SUCCESS", total_runs=9)
    a["runEvents"]["runEvents"].append(
        {"timestampMillis": 1, "runId": "old", "status": "COMPLETE",
         "result": {"type": "FAILURE"}})
    r = AssertionCorroborator(_gql_for([a])).corroborate(URN)
    assert r.verdict is Corroboration.CORROBORATED


# --------------------------------------------------------------------------- #
# Failure modes — none may read as agreement
# --------------------------------------------------------------------------- #

def test_no_transport_is_not_corroboration():
    r = AssertionCorroborator(None).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED
    assert "GraphQL-only" in r.reason


def test_a_raising_transport_is_not_corroboration_and_does_not_propagate():
    """
    A corroborator that throws turns "the catalog was unreachable" into a
    failed incident response.
    """
    def _boom(q, v):
        raise ConnectionError("gms unreachable")

    r = AssertionCorroborator(_boom).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED
    assert "ConnectionError" in r.reason


def test_graphql_errors_are_not_corroboration():
    r = AssertionCorroborator(
        lambda q, v: {"errors": [{"message": "unauthorized"}], "data": None}
    ).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED


def test_a_missing_dataset_is_not_corroboration():
    r = AssertionCorroborator(lambda q, v: {"data": {"dataset": None}}).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED


def test_an_empty_payload_is_not_corroboration():
    r = AssertionCorroborator(lambda q, v: {}).corroborate(URN)
    assert r.verdict is Corroboration.NOT_CORROBORATED


def test_truncation_is_reported():
    r = AssertionCorroborator(
        _gql_for([_assertion("a", "SUCCESS")], total=99)
    ).corroborate(URN)
    assert r.truncated
    assert r.total_assertions == 99


# --------------------------------------------------------------------------- #
# The two-source rule
# --------------------------------------------------------------------------- #

def test_both_sources_must_agree():
    good = AssertionCorroborator(_gql_for([_assertion("a", "SUCCESS")])).corroborate(URN)
    agreed, reason = recovery_is_agreed(True, good)
    assert agreed and "every catalog assertion agrees" in reason


def test_a_runtime_failure_is_never_rescued_by_the_catalog():
    good = AssertionCorroborator(_gql_for([_assertion("a", "SUCCESS")])).corroborate(URN)
    agreed, reason = recovery_is_agreed(False, good)
    assert not agreed
    assert "runtime re-run did not pass" in reason


def test_the_catalog_outranks_a_local_pass():
    """
    The whole point. DevGuard proposed the fix, ran the test and graded it; a
    contradiction from an independent source must win.
    """
    bad = AssertionCorroborator(_gql_for([_assertion("a", "FAILURE")])).corroborate(URN)
    agreed, reason = recovery_is_agreed(True, bad)
    assert not agreed
    assert "contradicts recovery" in reason


def test_an_unavailable_catalog_blocks_agreement():
    """Not-corroborated is not agreement, however convenient that would be."""
    agreed, reason = recovery_is_agreed(True, AssertionCorroborator(None).corroborate(URN))
    assert not agreed
    assert "cannot corroborate" in reason


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

def test_evidence_is_datahub_graph_when_the_catalog_answered():
    """It came from the catalog, whether it agreed or disagreed."""
    b = EvidenceBuilder("INC-1")
    for assertions in ([_assertion("a", "SUCCESS")], [_assertion("a", "FAILURE")]):
        report = AssertionCorroborator(_gql_for(assertions)).corroborate(URN)
        ev = AssertionCorroborator(None).evidence(b, report, raw_ref="ref.json")
        assert ev.source is EvidenceSource.DATAHUB_GRAPH
        assert ev.datahub_urn == URN


def test_a_null_reading_refuses_to_become_chain_evidence():
    """
    The hazard that made this guard necessary: EvidenceChain.is_sufficient()
    requires >=1 RUNTIME and >=1 DATAHUB_GRAPH and checks *source alone*.
    Minting a NOT_CORROBORATED reading as DATAHUB_GRAPH would let a failed
    lookup satisfy the rule that exists to force two independent sources.
    Lowering confidence does not prevent that, so it must refuse.
    """
    b = EvidenceBuilder("INC-1")
    report = AssertionCorroborator(_gql_for([])).corroborate(URN)
    with pytest.raises(ValueError, match="NOT_CORROBORATED"):
        AssertionCorroborator(None).evidence(b, report, raw_ref="ref.json")


def test_the_chain_rule_really_does_ignore_confidence():
    """
    Pins the premise of the guard above, behaviourally: a chain holding one
    RUNTIME item and one INFERRED DATAHUB_GRAPH item is already "sufficient".
    That is exactly why a null reading must not be minted as DATAHUB_GRAPH.
    """
    from backend.v2.evidence import EvidenceChain, EvidenceTrust

    b = EvidenceBuilder("INC-1")
    runtime = b.make(source=EvidenceSource.RUNTIME, trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.OBSERVED,
                     claim="dbt run exited 1", raw_ref="r.txt")
    weakest = b.make(source=EvidenceSource.DATAHUB_GRAPH, trust=EvidenceTrust.TRUSTED_SYSTEM,
                     confidence=EvidenceConfidence.INFERRED,
                     claim="catalog said nothing", raw_ref="g.json")
    chain = EvidenceChain(incident_id="INC-1", items=()).add(runtime, weakest)
    assert chain.is_sufficient, (
        "if this now fails, is_sufficient() weighs confidence and the refusal "
        "in AssertionCorroborator.evidence() can be reconsidered"
    )


def test_a_contradiction_is_observed_not_inferred():
    """A failing assertion is a strong, trustworthy signal — just a negative one."""
    b = EvidenceBuilder("INC-1")
    report = AssertionCorroborator(_gql_for([_assertion("a", "FAILURE")])).corroborate(URN)
    ev = AssertionCorroborator(None).evidence(b, report, raw_ref="ref.json")
    assert ev.confidence is EvidenceConfidence.OBSERVED


# --------------------------------------------------------------------------- #
# The query itself
# --------------------------------------------------------------------------- #

def test_the_query_uses_only_real_schema_fields():
    """
    Field names verified against DataHub's published entity.graphql. A typo
    here fails only against a live catalog, which is the one place this
    repository cannot test.
    """
    for fragment in ("dataset(urn: $urn)", "assertions(start: 0, count: $count)",
                     "runEvents(status: COMPLETE, limit: $limit)",
                     "result { type }", "timestampMillis", "runId"):
        assert fragment in ASSERTIONS_QUERY


def test_the_query_is_read_only():
    """No mutation may reach the catalog through this path."""
    assert ASSERTIONS_QUERY.lstrip().startswith("query")
    assert "mutation" not in ASSERTIONS_QUERY


def test_the_corroborator_is_passed_the_urn_it_was_asked_about():
    seen = {}

    def _gql(query, variables):
        seen.update(variables)
        return {"data": {"dataset": None}}

    AssertionCorroborator(_gql).corroborate(URN)
    assert seen["urn"] == URN
    assert seen["count"] > 0 and seen["limit"] > 0


# --------------------------------------------------------------------------- #
# The wiring — without this the module above is a library nobody calls
# --------------------------------------------------------------------------- #

class _Corroborator:
    """Records the URN it was asked about and returns a fixed verdict."""

    def __init__(self, assertions):
        self._gql = _gql_for(assertions)
        self.asked = []

    def corroborate(self, dataset_urn):
        self.asked.append(dataset_urn)
        return AssertionCorroborator(self._gql).corroborate(dataset_urn)


def _scribe(tmp_path, corroborator, resolved):
    from tests.test_writeback_rules import FakeClient
    from backend.v2.agents.scribe import Scribe
    from backend.v2.proofpack import ProofPack

    return Scribe(
        FakeClient(ok=True), None, ProofPack(tmp_path, "r"),
        gql=lambda q, v: resolved.append(v) or {"data": {"updateIncidentStatus": True}},
        corroborator=corroborator,
    )


def _write_back(scribe):
    from tests.test_writeback_rules import CHAIN, DATASET, VERIFIED, approved_request

    return scribe.write_back(
        chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
        dataset_urn=DATASET, column="user_id",
        incident_urn="urn:li:incident:i", root_cause="rc")


def test_a_contradicting_catalog_holds_the_incident_active(tmp_path):
    """
    The wiring, proven at the Scribe boundary. Runtime passed and every
    knowledge artifact landed — the only thing standing between this and a
    RESOLVED incident is the catalog's disagreement.
    """
    resolved: list = []
    c = _Corroborator([_assertion("unique_user_id", "FAILURE")])
    result = _write_back(_scribe(tmp_path, c, resolved))

    assert result.incident_resolved is False
    assert resolved == [], "the resolve mutation must not even be attempted"
    assert c.asked, "the corroborator was never consulted"


def test_an_agreeing_catalog_lets_the_incident_resolve(tmp_path):
    """The converse — otherwise the gate above would just block everything."""
    resolved: list = []
    c = _Corroborator([_assertion("unique_user_id", "SUCCESS")])
    result = _write_back(_scribe(tmp_path, c, resolved))

    assert result.incident_resolved is True
    assert len(resolved) == 1


def test_the_corroborator_is_asked_about_the_dataset_not_the_incident(tmp_path):
    """Assertions hang off the dataset; asking about the incident URN returns nothing."""
    from tests.test_writeback_rules import DATASET

    c = _Corroborator([_assertion("a", "SUCCESS")])
    _write_back(_scribe(tmp_path, c, []))
    assert c.asked == [DATASET]


def test_without_a_corroborator_behaviour_is_unchanged(tmp_path):
    """
    Backwards compatibility, deliberately. Every run recorded before this
    existed was captured without a corroborator; making it mandatory would
    retroactively turn those into un-resolvable incidents.
    """
    resolved: list = []
    result = _write_back(_scribe(tmp_path, None, resolved))
    assert result.incident_resolved is True
    assert len(resolved) == 1
