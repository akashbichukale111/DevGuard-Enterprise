"""
Pipeline reflection-loop tests (Checks: "pipeline tests: converges ·
exhausts retries · circuit breaker opens").

Every test here drives the real `run_pipeline` with the three agent calls
monkeypatched, so the loop's control flow is exercised with no API key, no
network, and no cost. What is under test is the orchestration — how many times
the Fixer runs, whether feedback is threaded, when it converges — not the
model's judgement.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import ai_agent
from backend.core.schemas import (
    FixResult,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

CLEAN_CODE = 'def get_user(uid):\n    return db.execute("SELECT 1 WHERE id = %s", (uid,))\n'
VULN_CODE = 'def get_user(uid):\n    return db.execute("SELECT 1 WHERE id = " + uid)\n'


def _vuln(severity=Severity.CRITICAL) -> Vulnerability:
    return Vulnerability(
        cwe_id="CWE-89",
        severity=severity,
        line_number=2,
        explanation="Raw concatenation reaches a SQL execute call.",
        confidence_score=0.94,
    )


def _scan(vulns) -> ScanResult:
    return ScanResult(
        vulnerabilities=vulns,
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=["CWE-89"],
    )


def _fix(patched: str = "patched") -> FixResult:
    return FixResult(
        patched_code=patched,
        diff_summary="Parameterised the query.",
        addressed_cwe_ids=["CWE-89"],
        model_used="llama-3.3-70b-versatile",
    )


def _validation(score: int, verdict: Verdict) -> ValidationResult:
    return ValidationResult(
        eval_score=score,
        verdict=verdict,
        reasoning="Reviewed the patch against the reported findings in detail.",
        unresolved_cwe_ids=[] if verdict is Verdict.PASS else ["CWE-89"],
        feedback="Looks correct." if verdict is Verdict.PASS else "Still concatenating.",
    )


@pytest.fixture
def spy(monkeypatch):
    """Monkeypatch the three agents and record how they were called."""
    calls = {"scanner": 0, "fixer": 0, "validator": 0}
    fixer_feedback: list = []

    state = {
        "scan": _scan([_vuln()]),
        "validations": [_validation(92, Verdict.PASS)],
    }

    # Signature mirrors the real `run_scanner`, including `override_model` — the
    # resilience layer's degraded path forces a model through the whole pipeline,
    # so a mock without it would pass while the real call site raised TypeError.
    async def fake_scanner(code, k_context=4, override_model=None):
        calls["scanner"] += 1
        calls.setdefault("scanner_override", []).append(override_model)
        return state["scan"]

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        calls["fixer"] += 1
        fixer_feedback.append(prior_feedback)
        return _fix()

    async def fake_validator(original_code, vulns, fix):
        calls["validator"] += 1
        i = min(calls["validator"] - 1, len(state["validations"]) - 1)
        return state["validations"][i]

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    monkeypatch.setattr(ai_agent, "run_fixer", fake_fixer)
    monkeypatch.setattr(ai_agent, "run_validator", fake_validator)
    # Keep the self-observation layer out of the way; it is tested separately.
    monkeypatch.setattr(
        ai_agent, "_select_model_adaptive", lambda sev: _noop_adaptive(sev)
    )
    return calls, fixer_feedback, state


async def _noop_adaptive(severity):
    return None, None


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #

def test_converges_on_first_pass(spy):
    calls, _, _ = spy
    result = run(ai_agent.run_pipeline(VULN_CODE))

    assert result.converged is True
    assert calls["fixer"] == 1
    assert calls["validator"] == 1
    assert len(result.reflection_history) == 1


def test_retries_then_converges(spy):
    calls, feedback, state = spy
    state["validations"] = [
        _validation(40, Verdict.FAIL),
        _validation(91, Verdict.PASS),
    ]

    result = run(ai_agent.run_pipeline(VULN_CODE))

    assert result.converged is True
    assert calls["fixer"] == 2
    assert len(result.reflection_history) == 2
    # The second Fixer call must have received the reviewer's feedback —
    # otherwise the "reflection" loop is just a retry loop.
    assert feedback[0] is None
    assert feedback[1] is not None
    assert "Still concatenating" in feedback[1]


def test_exhausts_retries_without_converging(spy):
    calls, _, state = spy
    state["validations"] = [_validation(30, Verdict.FAIL)]

    result = run(ai_agent.run_pipeline(VULN_CODE))

    assert result.converged is False
    assert calls["fixer"] == ai_agent.MAX_REFLECTION_RETRIES
    assert len(result.reflection_history) == ai_agent.MAX_REFLECTION_RETRIES


def test_a_high_score_with_a_fail_verdict_does_not_converge(spy):
    """Both conditions gate convergence. schemas.py separates score from verdict
    deliberately, so a well-scoring patch the reviewer still rejects must keep
    looping rather than shipping."""
    calls, _, state = spy
    state["validations"] = [_validation(99, Verdict.FAIL)]

    result = run(ai_agent.run_pipeline(VULN_CODE))

    assert result.converged is False
    assert calls["fixer"] == ai_agent.MAX_REFLECTION_RETRIES


def test_a_pass_verdict_below_the_threshold_does_not_converge(spy):
    calls, _, state = spy
    state["validations"] = [_validation(ai_agent.EVAL_PASS_THRESHOLD - 1, Verdict.PASS)]

    result = run(ai_agent.run_pipeline(VULN_CODE))
    assert result.converged is False


def test_exactly_at_the_threshold_converges(spy):
    """Boundary: the gate is >=, so the threshold value itself must pass."""
    calls, _, state = spy
    state["validations"] = [_validation(ai_agent.EVAL_PASS_THRESHOLD, Verdict.PASS)]

    result = run(ai_agent.run_pipeline(VULN_CODE))
    assert result.converged is True
    assert calls["fixer"] == 1


# --------------------------------------------------------------------------- #
# Clean code — the negative-control path
# --------------------------------------------------------------------------- #

def test_clean_code_does_not_invoke_the_fixer(spy):
    """A scan that finds nothing must not run the Fixer or the Validator.

    benchmark.py ships a negative control (`clean_parameterized`), so clean
    input is an expected path, not an edge case. Running the loop anyway costs
    up to 2 x MAX_REFLECTION_RETRIES paid LLM calls and asks the Fixer to
    "harden" code with no reported findings — which means rewriting code that
    was already correct.
    """
    calls, _, state = spy
    state["scan"] = _scan([])

    result = run(ai_agent.run_pipeline(CLEAN_CODE))

    assert calls["scanner"] == 1
    assert calls["fixer"] == 0, (
        f"Fixer ran {calls['fixer']} time(s) on code with no reported "
        "vulnerabilities — that is wasted spend and an unnecessary rewrite"
    )
    assert calls["validator"] == 0


def test_clean_code_returns_the_original_code_unmodified(spy):
    _, _, state = spy
    state["scan"] = _scan([])

    result = run(ai_agent.run_pipeline(CLEAN_CODE))

    assert result.original_code == CLEAN_CODE
    assert result.final_fix.patched_code == CLEAN_CODE
    assert result.final_fix.addressed_cwe_ids == []


def test_clean_code_is_not_reported_as_a_model_judgement(spy):
    """The no-op result must not masquerade as a Validator verdict.

    the no-fabrication rule: no model graded this code, so no eval_score may imply one did.
    final_validation is None rather than a synthesised pass.
    """
    _, _, state = spy
    state["scan"] = _scan([])

    result = run(ai_agent.run_pipeline(CLEAN_CODE))

    assert result.final_validation is None, (
        "a synthesised ValidationResult would present an invented eval_score as "
        "a Validator judgement"
    )
    assert "none" in result.final_fix.model_used.lower()
    assert result.reflection_history == []


def test_clean_code_still_reports_converged(spy):
    """Nothing to fix is a successful outcome, not a failure to converge."""
    _, _, state = spy
    state["scan"] = _scan([])
    assert run(ai_agent.run_pipeline(CLEAN_CODE)).converged is True


def test_self_observation_logs_do_not_claim_a_signoz_round_trip(caplog):
    """The provenance rule, in the log stream.

    These records are exported to the collector, so they land in SigNoz itself.
    They used to read "Querying SigNoz MCP Server ..." and "SigNoz MCP Server
    reports ..." while no MCP server was configured or contacted — asserting a
    round trip that never happened, on the surface a judge would inspect.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="devguard.self_observer.mcp"):
        ai_agent._safe_is_conservation_mode()

    joined = " ".join(r.message for r in caplog.records)
    assert joined, "expected at least one self-observation log record"
    assert "SigNoz MCP Server" not in joined, (
        f"log claims a SigNoz MCP round trip that did not happen: {joined!r}"
    )
