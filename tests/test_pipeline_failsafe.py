"""
The fail-safe requirement — "Fail-safe stays fail-safe. SigNoz down must never break a scan. Prove
it with a test that runs the pipeline with the collector unreachable."

`tests/test_telemetry_failsafe.py` already covers the ENDPOINTS with a dead
collector. It does not cover the reflection pipeline itself — `run_pipeline` was
never exercised in that state, so the contract's actual wording ("runs the
pipeline") had no test behind it. This file closes that gap.

WHAT IS AND IS NOT UNDER TEST. The three agent calls are monkeypatched, exactly
as in test_pipeline_loop.py, so no API key, network or spend is involved. What is
being proven is that the ORCHESTRATION plus the TELEMETRY LAYER stay correct when
the OTLP exporter cannot reach anything: the loop still converges, the result is
unchanged, and no exporter error escapes into the caller's path. The models'
judgement is not under test here and never has been.

WHY THIS IS A REAL RISK, not a formality. Telemetry is initialised at import and
wrapped around every agent call. A `BatchSpanProcessor` whose exporter refuses
connections retries on a background thread; if any of that leaked synchronously —
an unhandled `ConnectionError`, a blocking flush, a span context manager that
re-raises on `__exit__` — a scan would fail *because observability* failed, which
is precisely the inversion the contract forbids.
"""

from __future__ import annotations

import asyncio
import os

import pytest

# The collector must be unreachable BEFORE backend.core.telemetry is imported,
# because the exporter endpoint is read at provider construction. Port 9 is the
# discard port: RFC 863, reserved, and reliably refuses TCP.
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:9"
# Force the batch processor to actually attempt an export during the test rather
# than sitting on its default 5s schedule and never trying.
os.environ["OTEL_BSP_SCHEDULE_DELAY"] = "200"
os.environ.pop("OTEL_SDK_DISABLED", None)

from backend.core import ai_agent  # noqa: E402
from backend.core.schemas import (  # noqa: E402
    FixResult,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

VULN_CODE = 'def get_user(uid):\n    return db.execute("SELECT 1 WHERE id = " + uid)\n'


def _vuln() -> Vulnerability:
    return Vulnerability(
        cwe_id="CWE-89",
        severity=Severity.CRITICAL,
        line_number=2,
        explanation="Raw concatenation reaches a SQL execute call.",
        confidence_score=0.94,
    )


def _scan() -> ScanResult:
    return ScanResult(
        vulnerabilities=[_vuln()],
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=["CWE-89"],
    )


def _fix() -> FixResult:
    return FixResult(
        patched_code="patched",
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


async def _noop_adaptive(severity):
    return None, None


@pytest.fixture
def agents(monkeypatch):
    """Monkeypatch the three agents; signatures mirror the real ones."""
    calls = {"scanner": 0, "fixer": 0, "validator": 0}
    verdicts = [_validation(92, Verdict.PASS)]

    async def fake_scanner(code, k_context=4, override_model=None):
        calls["scanner"] += 1
        return _scan()

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        calls["fixer"] += 1
        return _fix()

    async def fake_validator(original_code, vulns, fix):
        calls["validator"] += 1
        i = min(calls["validator"] - 1, len(verdicts) - 1)
        return verdicts[i]

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    monkeypatch.setattr(ai_agent, "run_fixer", fake_fixer)
    monkeypatch.setattr(ai_agent, "run_validator", fake_validator)
    monkeypatch.setattr(ai_agent, "_select_model_adaptive", _noop_adaptive)
    return calls, verdicts


def test_the_collector_really_is_unreachable():
    """Guard the guard.

    If the endpoint were reachable, or the SDK disabled, every other test in this
    file would pass for the wrong reason — they would prove nothing about
    fail-safety. This asserts the hostile condition is actually in force before
    anything else claims to survive it.
    """
    import socket

    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:9"
    assert os.environ.get("OTEL_SDK_DISABLED") in (None, "", "false")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        with pytest.raises(OSError):
            s.connect(("127.0.0.1", 9))


def test_pipeline_converges_with_collector_unreachable(agents):
    """The contract's actual sentence: run the pipeline, collector down."""
    calls, _ = agents
    result = asyncio.run(ai_agent.run_pipeline(VULN_CODE))

    assert result is not None
    assert calls["scanner"] == 1
    assert calls["fixer"] >= 1
    assert calls["validator"] >= 1

    # The verdict must be the real one, not a telemetry-degraded placeholder.
    assert result.final_validation is not None
    assert result.final_validation.verdict is Verdict.PASS
    assert result.final_validation.eval_score == 92


def test_pipeline_result_is_identical_to_the_healthy_case(agents):
    """Fail-safe means UNCHANGED, not merely 'did not crash'.

    A pipeline that silently dropped a retry, or returned a truncated history,
    would still 'not crash' — and would still be a regression. This pins the
    observable result, not just the absence of an exception.
    """
    _, _ = agents
    result = asyncio.run(ai_agent.run_pipeline(VULN_CODE))

    assert result.scan is not None
    assert len(result.scan.vulnerabilities) == 1
    assert result.scan.vulnerabilities[0].cwe_id == "CWE-89"
    assert result.final_fix is not None
    assert result.final_fix.patched_code == "patched"
    # History and convergence are retained even though every span export failed.
    # `reflection_history` is the real field — an earlier draft of this test
    # asserted `result.attempts`, which PipelineResult does not have. Pydantic
    # raised AttributeError and the test failed loudly, which is exactly the
    # behaviour the codebase's own `getattr(obj, "field", default)` sweep was
    # about: a silent default here would have made this assertion vacuous.
    assert len(result.reflection_history) >= 1
    assert result.converged is True


def test_repeated_scans_do_not_degrade_with_a_dead_collector(agents):
    """Export failures accumulate in the batch processor's queue.

    A leak or an unbounded retry would show up as the Nth scan behaving
    differently from the first. Ten consecutive runs must be indistinguishable.
    """
    calls, _ = agents
    for _ in range(10):
        result = asyncio.run(ai_agent.run_pipeline(VULN_CODE))
        assert result.final_validation.verdict is Verdict.PASS

    assert calls["scanner"] == 10


def test_no_exporter_error_escapes_into_the_caller(agents):
    """No ConnectionError/OSError from the exporter may surface to the caller.

    Asserted explicitly rather than relying on the other tests' silence: a
    background-thread exporter failure that got marshalled onto the caller's
    stack would fail here with the exporter's own exception type, which is a much
    clearer diagnosis than 'some test went red'.
    """
    _, _ = agents
    try:
        result = asyncio.run(ai_agent.run_pipeline(VULN_CODE))
    except (ConnectionError, OSError) as exc:  # pragma: no cover - the bug case
        pytest.fail(
            "A telemetry transport error reached the caller, so observability is "
            f"load-bearing on the scan path: {exc!r}"
        )
    assert result is not None
