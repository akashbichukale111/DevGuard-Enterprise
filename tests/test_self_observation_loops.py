"""
Which of the four advertised self-observation loops actually fire.

The README presents four loops as a table of working behaviour, and that table is
the project's headline differentiator. Two of the four do fire. Two do not, for
different reasons, and neither the README nor the code said so.

THE OVERCLAIM THESE TESTS EXIST FOR
-----------------------------------
README, "The Differentiator: Self-Observation":

    | Pattern-Learning Agent | Historical Validator fail-rate per CWE |
      Elevates RAG retrieval depth (k) for CWE classes with a track record of
      needing more context |

Two things are wrong with that as a statement about a scan:

1. **It is not wired into the scan pipeline at all.** `ai_agent.py` imports
   `suggest_context_k` with `# noqa: F401 (kept imported; see run_pipeline note
   on why it's not called yet)`. `run_pipeline` calls `run_scanner(code)` with no
   `k_context`, so retrieval depth is always the default 4, and both return paths
   hard-code `context_k_adjusted=False`. The only caller is
   `god_mode_orchestrator.execute_llm_judge` — a Nexus endpoint the README itself
   describes as running synthetically.

2. **It cannot fire anywhere, even where it is called.** `suggest_context_k` asks
   `mcp_client.get_cwe_failure_pattern` for each candidate CWE. That query has no
   local fallback — unlike `get_recent_cost_trend`, which falls back to
   `local_telemetry` — so with no SigNoz MCP server it returns
   `PatternStats(available=False)` every time, `max_failure_rate` stays 0.0, and
   the function returns `base_k`. Measured: `suggest_context_k(["CWE-89"],
   base_k=4)` returns **4**, never the configured `ELEVATED_CONTEXT_K` of 8.

So the loop is blocked on the same unverified MCP integration as the rest of that layer,
not merely unwired. These tests pin the true behaviour so the claim and the code
cannot drift apart again, and so that wiring it up later fails a test rather than
passing silently.

WHY THIS IS NOT "FIXED" HERE: making it fire means changing the Scanner's prompt
context on the main detection path. Whether more context improves or degrades
detection is exactly the thing that needs a live LLM key to measure, and there is
none (open issue 2). Shipping an unmeasurable behaviour change to the most
important path would be the wrong call; documenting it accurately is not.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core import ai_agent, self_observer
from backend.core.schemas import (
    FixResult,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Loop 3, Pattern-Learning: inert, and honestly reported as such
# --------------------------------------------------------------------------- #

def test_suggest_context_k_cannot_elevate_without_mcp():
    """With no MCP server, every CWE lookup is unavailable, so k stays at base."""
    assert self_observer.ELEVATED_CONTEXT_K != 4, "pick a base_k that differs"
    assert run(self_observer.suggest_context_k(["CWE-89", "CWE-79"], base_k=4)) == 4


def test_suggest_context_k_does_elevate_when_the_history_is_available():
    """The logic itself is correct — it is the data source that is missing.

    Worth pinning separately, so the "blocked on MCP" diagnosis stays honest: if
    this ever fails, the loop is broken as well as blocked.
    """
    class _Pattern:
        available = True

        def __init__(self, rate):
            self.fail_rate = rate

    class _Client:
        async def get_cwe_failure_pattern(self, cwe_id):
            return _Pattern(0.9 if cwe_id == "CWE-89" else 0.0)

    import backend.core.self_observer as so

    original = so.get_mcp_client
    so.get_mcp_client = lambda: _Client()
    try:
        assert run(so.suggest_context_k(["CWE-89"], base_k=4)) == so.ELEVATED_CONTEXT_K
        # Below the threshold, no elevation.
        assert run(so.suggest_context_k(["CWE-79"], base_k=4)) == 4
    finally:
        so.get_mcp_client = original


def test_the_scan_pipeline_does_not_call_suggest_context_k():
    """Pins the documented state: the loop is not in the scan path.

    If someone wires it in, this fails — which is the point. Wiring it changes
    the Scanner's prompt context on the main detection path, so it must be a
    deliberate, measured change with the README updated in the same commit, not a
    quiet edit.
    """
    src = inspect.getsource(ai_agent.run_pipeline)
    assert "suggest_context_k" not in src, (
        "run_pipeline now calls suggest_context_k — update the README's "
        "Pattern-Learning row and this test together, and measure the effect on "
        "detection with a live key first"
    )


def test_the_pipeline_reports_context_k_as_unadjusted(monkeypatch):
    """`self_observation.context_k_adjusted` must not claim an adaptation that
    did not happen (the no-fabrication rule)."""
    async def fake_scanner(code, k_context=4, override_model=None):
        return ScanResult(
            vulnerabilities=[
                Vulnerability(
                    cwe_id="CWE-89",
                    severity=Severity.LOW,
                    line_number=1,
                    explanation="Concatenated user input reaches a SQL execute call.",
                    confidence_score=0.9,
                )
            ],
            model_used="llama-3.3-70b-versatile",
        )

    async def fake_fixer(code, vulns, prior_feedback=None, override_model=None):
        return FixResult(
            patched_code="ok",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="llama-3.3-70b-versatile",
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

    result = run(ai_agent.run_pipeline("q = 'x' + uid\n"))
    assert result.self_observation.context_k_adjusted is False


def test_the_scanner_retrieves_at_the_default_depth(monkeypatch):
    """The observable consequence: k is never elevated for a real scan."""
    seen: dict = {}

    class _Store:
        def retrieve(self, code, k=4):
            seen["k"] = k
            return []

    monkeypatch.setattr(ai_agent, "get_store", lambda: _Store())

    class _Resp:
        class _C:
            class _M:
                content = '{"vulnerabilities": []}'

            message = _M()

        choices = [_C()]
        usage = None

    async def fake_call_llm(**kwargs):
        return _Resp()

    monkeypatch.setattr(ai_agent, "_call_llm", fake_call_llm)
    run(ai_agent.run_scanner("q = 'x'\n"))
    assert seen["k"] == 4


def test_the_detailed_form_reports_whether_the_history_was_available():
    """`suggest_context_k` returns only an int, so a caller cannot tell whether
    a returned `base_k` means "history says base_k is enough" or "there was no
    history". Those are different facts, and the Truth Serum panel renders the
    value as `Recommended k` inside a payload badged `data_source: "live"`.
    """
    k, informed = run(self_observer.suggest_context_k_detailed(["CWE-89"], base_k=4))
    assert k == 4
    assert informed is False, (
        "reported the CWE history as consulted when no MCP server answered"
    )


def test_the_detailed_form_reports_informed_when_history_exists():
    class _Pattern:
        available = True
        fail_rate = 0.05

    class _Client:
        async def get_cwe_failure_pattern(self, cwe_id):
            return _Pattern()

    import backend.core.self_observer as so

    original = so.get_mcp_client
    so.get_mcp_client = lambda: _Client()
    try:
        k, informed = run(so.suggest_context_k_detailed(["CWE-89"], base_k=4))
        assert k == 4, "a 0.05 fail rate is below the threshold, so k stays at base"
        assert informed is True, "the history WAS consulted; that is the distinction"
    finally:
        so.get_mcp_client = original


def test_the_plain_form_still_returns_a_bare_int():
    """Backwards compatibility: `suggest_context_k` is imported by ai_agent and
    called by the orchestrator. Its contract must not change underneath them."""
    assert run(self_observer.suggest_context_k(["CWE-89"], base_k=4)) == 4


def test_the_truth_serum_payload_says_whether_k_was_informed():
    """The consequence, at the surface that renders it."""
    from backend.core import god_mode_orchestrator as gmo

    result = run(gmo.execute_llm_judge(cwe_id="CWE-89"))
    # No code supplied -> the synthetic branch, which must not claim otherwise.
    assert result["data_source"] == "synthetic"
    assert result["context_k_source"] == "not_consulted"


# --------------------------------------------------------------------------- #
# Loop 1, Telemetry-Aware Router: fires, and is already covered
# --------------------------------------------------------------------------- #

def test_the_router_loop_is_wired_into_the_pipeline():
    src = inspect.getsource(ai_agent.run_pipeline)
    assert "_select_model_adaptive" in src


# --------------------------------------------------------------------------- #
# Loop 4, CostGuardian: fires
# --------------------------------------------------------------------------- #

def test_the_cost_guardian_tick_is_wired_into_the_pipeline():
    src = inspect.getsource(ai_agent.run_pipeline)
    assert "record_scan()" in src


def test_conservation_mode_is_reported_from_the_real_flag(monkeypatch):
    """`conservation_mode_active` must reflect the guardian, not a constant."""
    monkeypatch.setattr(ai_agent, "_safe_is_conservation_mode", lambda: True)

    async def fake_scanner(code, k_context=4, override_model=None):
        return ScanResult(vulnerabilities=[], model_used="m")

    monkeypatch.setattr(ai_agent, "run_scanner", fake_scanner)
    result = run(ai_agent.run_pipeline("print('ok')\n"))
    assert result.self_observation.conservation_mode_active is True


# --------------------------------------------------------------------------- #
# Loop 2, Postmortem: fires on breaker open
# --------------------------------------------------------------------------- #

def test_the_postmortem_loop_is_wired_to_the_breaker():
    from backend.core import resilience

    src = inspect.getsource(resilience.CircuitBreaker._transition)
    assert "_schedule_postmortem" in src
