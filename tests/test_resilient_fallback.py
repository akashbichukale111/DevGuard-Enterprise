"""
Graceful degradation — does the fallback actually degrade to a different model?

README, Core Features:

    "Circuit breaker + graceful degradation with an AI-generated postmortem
     the moment it trips"

`resilience.py`'s own docstring:

    "SRE NOTE: We annotate the ScanResult / span with which model actually
     served the request ("served_by") and whether we degraded, so a judge (or
     on-call) can immediately see 'this answer came from the fallback' during an
     incident."

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
`_invoke(request, model_id)` took a model and threw it away:

    telemetry.trace.get_current_span().set_attribute("llm.model_id", model_id)
    return await run_pipeline(request.code)          # <- model_id unused

`run_pipeline` did its own severity-based routing with no override hook, so the
"fallback attempt" re-ran the *identical* models the primary attempt had just
used. Two consequences, and the second is worse:

1. It was a plain retry, not a degradation. The escape hatch offered no cheaper
   or differently-provisioned path — so a primary outage caused by the 70B model
   being unavailable would fail again for the same reason.

2. **The span lied about it.** The caller stamped
   `llm.served_by = FALLBACK_MODEL` and `llm.degraded = True` regardless. During
   an incident, a trace would state that `llama-3.1-8b-instant` served the
   request when the 70B model actually did. That attribute is exported to
   SigNoz, so the false claim lands on the exact surface an on-call engineer
   would trust, in the exact situation where being wrong costs the most.

`_invoke`'s docstring was candid about the mechanism ("record model_id only as a
span attribute ... even though the AI layer doesn't act on it directly"). The
caller then published it as fact anyway. the provenance rule: live or labelled.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import ai_agent, resilience
from backend.core.schemas import (
    AgentExecutionError,
    FixResult,
    PipelineResult,
    ScanRequest,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

CRITICAL_CODE = 'q = "SELECT * FROM t WHERE id = " + uid\ndb.execute(q)\n'


def run(coro):
    return asyncio.run(coro)


def _result(scanner_model: str, fixer_model: str) -> PipelineResult:
    scan = ScanResult(
        vulnerabilities=[
            Vulnerability(
                cwe_id="CWE-89",
                severity=Severity.CRITICAL,
                line_number=1,
                explanation="Concatenated user input reaches a SQL execute call.",
                confidence_score=0.94,
            )
        ],
        model_used=scanner_model,
        retrieved_cwe_ids=["CWE-89"],
    )
    return PipelineResult(
        original_code=CRITICAL_CODE,
        scan=scan,
        final_fix=FixResult(
            patched_code="db.execute(q, (uid,))",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used=fixer_model,
        ),
        final_validation=ValidationResult(
            eval_score=92,
            verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
        ),
        converged=True,
        routing_decisions={"scanner": scanner_model},
    )


@pytest.fixture(autouse=True)
def fresh_breaker(monkeypatch):
    """Every test gets a CLOSED breaker; module state must not leak between them."""
    monkeypatch.setattr(
        resilience, "_primary_breaker", resilience.CircuitBreaker(name="test_breaker")
    )
    # Postmortem generation reaches for an LLM; keep it out of the way.
    monkeypatch.setattr(resilience, "_get_postmortem_agent", lambda: None)


@pytest.fixture
def spy_pipeline(monkeypatch):
    """Record the model override `run_pipeline` is asked for on each attempt."""
    calls: list = []

    async def fake_pipeline(code, override_model=None):
        calls.append(override_model)
        # The first attempt fails; the fallback attempt succeeds.
        if len(calls) == 1:
            raise AgentExecutionError("scanner", "upstream 503")
        return _result(override_model or "unspecified", override_model or "unspecified")

    monkeypatch.setattr(resilience, "run_pipeline", fake_pipeline)
    return calls


# --------------------------------------------------------------------------- #
# The fallback must actually use a different model
# --------------------------------------------------------------------------- #

def test_the_fallback_attempt_requests_the_fallback_model(spy_pipeline):
    """The defect, directly.

    Two attempts, and they must not ask for the same model — otherwise this is a
    retry wearing a degradation label.
    """
    result = run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))

    assert len(spy_pipeline) == 2, f"expected primary + fallback, got {spy_pipeline}"
    assert spy_pipeline[0] != spy_pipeline[1], (
        f"both attempts requested the same model {spy_pipeline[0]!r} — the "
        "fallback does not degrade to anything"
    )
    assert spy_pipeline[1] == resilience.FALLBACK_MODEL


def test_the_primary_attempt_does_not_force_a_model(spy_pipeline):
    """The primary path must keep the pipeline's own severity-based routing.

    Forcing PRIMARY_MODEL on attempt 1 would override the safety-relevant
    per-agent routing (the Scanner deliberately always uses the strong model,
    the Fixer may be adapted by the self-observation layer). Only the *fallback*
    overrides, and only because degrading is the point.
    """
    run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))
    assert spy_pipeline[0] is None


def test_an_open_breaker_skips_straight_to_the_fallback_model(monkeypatch):
    calls: list = []

    async def fake_pipeline(code, override_model=None):
        calls.append(override_model)
        return _result("m", "m")

    monkeypatch.setattr(resilience, "run_pipeline", fake_pipeline)
    # Force the breaker OPEN so the primary path is suppressed entirely.
    resilience._primary_breaker._state = resilience.CircuitState.OPEN
    resilience._primary_breaker._opened_at = float("inf")

    run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))
    assert calls == [resilience.FALLBACK_MODEL], (
        "an OPEN breaker must go straight to the fallback model, not retry the "
        f"primary routing; got {calls}"
    )


# --------------------------------------------------------------------------- #
# served_by / degraded must describe what happened, not what was intended
# --------------------------------------------------------------------------- #

class _RecordingSpan:
    def __init__(self):
        self.attrs: dict = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def record_exception(self, *a, **kw):
        pass

    def set_status(self, *a, **kw):
        pass

    def add_event(self, *a, **kw):
        pass


@pytest.fixture
def span(monkeypatch):
    """Capture the span attributes `run_pipeline_resilient` writes.

    Patched on the `resilience` module's own reference rather than globally: the
    OTel SDK calls `trace.get_current_span(context)` internally while starting
    real spans, so replacing it library-wide breaks span creation. This keeps the
    patch to the one call site under test.
    """
    s = _RecordingSpan()
    real_trace = resilience.telemetry.trace

    class _TraceShim:
        def __getattr__(self, name):
            return getattr(real_trace, name)

        @staticmethod
        def get_current_span(*_args, **_kwargs):
            return s

    monkeypatch.setattr(resilience.telemetry, "trace", _TraceShim())
    return s


def test_a_successful_primary_reports_the_model_the_pipeline_actually_used(
    span, monkeypatch
):
    """`served_by` must be the model that ran, not the constant PRIMARY_MODEL.

    The pipeline routes per agent and the self-observation layer can adapt the
    Fixer, so PRIMARY_MODEL is an assumption. Reporting the Scanner's real
    `model_used` makes the attribute a measurement.
    """
    async def fake_pipeline(code, override_model=None):
        return _result("llama-3.3-70b-versatile", "llama-3.3-70b-versatile")

    monkeypatch.setattr(resilience, "run_pipeline", fake_pipeline)
    run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))

    assert span.attrs["llm.degraded"] is False
    assert span.attrs["llm.served_by"] == "llama-3.3-70b-versatile"


def test_the_fallback_reports_the_model_that_really_served_it(span, monkeypatch):
    """The core lie: `served_by` was stamped from a constant.

    Here the fallback pipeline reports it used a *third* model. The span must say
    so rather than asserting FALLBACK_MODEL, because an on-call engineer reads
    this attribute to find out what actually answered.
    """
    calls: list = []

    async def fake_pipeline(code, override_model=None):
        calls.append(override_model)
        if len(calls) == 1:
            raise AgentExecutionError("scanner", "upstream 503")
        return _result("some-other-model-entirely", "some-other-model-entirely")

    monkeypatch.setattr(resilience, "run_pipeline", fake_pipeline)
    run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))

    assert span.attrs["llm.degraded"] is True
    assert span.attrs["llm.served_by"] == "some-other-model-entirely", (
        "the span reported a model that did not serve the request: "
        f"{span.attrs['llm.served_by']!r}"
    )
    # And the requested override is recorded separately from what served it,
    # so intent and outcome stay distinguishable.
    assert span.attrs["llm.fallback_model_requested"] == resilience.FALLBACK_MODEL


def test_total_failure_reports_served_by_none_and_reraises(span, monkeypatch):
    async def always_fails(code, override_model=None):
        raise AgentExecutionError("scanner", "everything is down")

    monkeypatch.setattr(resilience, "run_pipeline", always_fails)

    with pytest.raises(AgentExecutionError):
        run(resilience.run_pipeline_resilient(ScanRequest(code=CRITICAL_CODE)))

    assert span.attrs["llm.served_by"] == "none"
    assert span.attrs["llm.degraded"] is True


# --------------------------------------------------------------------------- #
# The override must reach the agents, not just the span
# --------------------------------------------------------------------------- #

def test_run_pipeline_threads_the_override_into_the_fixer(monkeypatch):
    """End-to-end on the override, with the agents mocked.

    A degradation that only changes a span attribute is not a degradation. This
    asserts the model reaches the call the Fixer actually makes.
    """
    seen: dict = {}

    async def fake_scanner(code, k_context=4, override_model=None):
        seen["scanner"] = override_model
        return _result("recorded-by-scanner", "x").scan

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        seen["fixer"] = override_model
        return FixResult(
            patched_code="fixed",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used=override_model or "unspecified",
        )

    async def fake_validator(original_code, vulns, fix):
        return ValidationResult(
            eval_score=95,
            verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
        )

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    monkeypatch.setattr(ai_agent, "run_fixer", fake_fixer)
    monkeypatch.setattr(ai_agent, "run_validator", fake_validator)

    run(ai_agent.run_pipeline(CRITICAL_CODE, override_model="forced-cheap-model"))

    assert seen["fixer"] == "forced-cheap-model", (
        "the degradation model never reached the Fixer"
    )
    assert seen["scanner"] == "forced-cheap-model", (
        "the degradation model never reached the Scanner"
    )


def test_no_override_leaves_severity_routing_alone(monkeypatch):
    """The default path must be unchanged — the override is opt-in only."""
    seen: dict = {}

    async def fake_scanner(code, k_context=4, override_model=None):
        seen["scanner"] = override_model
        return _result("m", "m").scan

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        seen["fixer"] = override_model
        return FixResult(
            patched_code="fixed",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="m",
        )

    async def fake_validator(original_code, vulns, fix):
        return ValidationResult(
            eval_score=95, verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
        )

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    monkeypatch.setattr(ai_agent, "run_fixer", fake_fixer)
    monkeypatch.setattr(ai_agent, "run_validator", fake_validator)

    run(ai_agent.run_pipeline(CRITICAL_CODE))
    assert seen["scanner"] is None
    # The Fixer may still receive an adaptive model from the self-observation
    # layer; what must not happen is a resilience override appearing from nowhere.
    assert seen["fixer"] in (None, ai_agent.MODEL_STRONG, ai_agent.MODEL_CHEAP)


# --------------------------------------------------------------------------- #
# Degrading must never override the critical-severity floor
# --------------------------------------------------------------------------- #

def test_the_scanner_override_is_still_recorded_in_routing_decisions(monkeypatch):
    """Auditability: if a degradation changed the model, the result must say so.

    `routing_decisions` is documented as "agent_name -> model_used, so routing is
    fully auditable". A silent degradation would make that record wrong.
    """
    async def fake_scanner(code, k_context=4, override_model=None):
        r = _result(override_model or "default", "x")
        return r.scan

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        return FixResult(
            patched_code="fixed",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used=override_model or "default",
        )

    async def fake_validator(original_code, vulns, fix):
        return ValidationResult(
            eval_score=95, verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
        )

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    monkeypatch.setattr(ai_agent, "run_fixer", fake_fixer)
    monkeypatch.setattr(ai_agent, "run_validator", fake_validator)

    result = run(ai_agent.run_pipeline(CRITICAL_CODE, override_model="degraded-model"))
    assert result.routing_decisions["scanner"] == "degraded-model"
    assert result.final_fix.model_used == "degraded-model"
