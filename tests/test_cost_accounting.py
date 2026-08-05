"""
Per-request cost and token accounting.

The Result Dashboard renders `cost_usd` as a "Cost" metric card, and the
`cost_calc` span carries `llm.prompt_tokens`, `llm.completion_tokens` and
`llm.cost_usd` into SigNoz.

THE DEFECTS THESE TESTS EXIST FOR
---------------------------------
Three helpers in `router.py` were written against `ScanResult` and are handed a
`PipelineResult`. Their type annotations still said `result: ScanResult`, which
is the tell. `PipelineResult` has none of the fields they read:

    PipelineResult fields: converged, final_fix, final_validation,
                           original_code, reflection_history,
                           routing_decisions, scan, self_observation

1. `_compute_and_record_cost` reads `result.model_id`, `result.prompt_tokens`,
   `result.completion_tokens`. All three `getattr` calls fell through to their
   defaults on every scan, so it computed
   `compute_cost_usd("__default__", 0, 0)` = **0.0, always**. Every `/scan`
   response reported `cost_usd: 0.0`, and every `cost_calc` span went to SigNoz
   claiming zero tokens and zero cost — for requests that had just made up to
   seven paid LLM calls.

2. `_emit_request_metrics` reads the same two token fields, so `total_tokens` was
   always 0 and the `if total_tokens and ...` guard never passed. The
   `devguard.llm.tokens_per_sec` histogram documented in `telemetry.py` has
   therefore never received a single observation.

3. `_extract_score` reads `eval_score`/`score`/`validator_score`, none of which
   exist either — but it has a fallback to `final_validation.eval_score`, so it
   works by accident. Tested here so the accident is pinned as intended
   behaviour.

Note the real per-call cost was always recorded correctly inside `_call_llm` ->
`record_llm_observability`. So the aggregate SigNoz counters were right while the
per-request span and the API response said zero — two sources disagreeing, with
the one a human reads being the wrong one.
"""

from __future__ import annotations

import pytest

from backend.api import router as api_router
from backend.core import telemetry
from backend.core.schemas import (
    FixResult,
    PipelineResult,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

CODE = 'q = "SELECT * FROM t WHERE id = " + uid\n'


class _Span:
    def __init__(self):
        self.attrs: dict = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


def _pipeline(
    scan_tokens=(900, 512),
    fix_tokens=(600, 300),
    val_tokens=(200, 140),
    model="llama-3.3-70b-versatile",
) -> PipelineResult:
    def _mk(t):
        return None if t is None else sum(t)

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
        model_used=model,
        tokens_used=_mk(scan_tokens),
        cost_usd=(
            None
            if scan_tokens is None
            else telemetry.compute_cost_usd(model, *scan_tokens)
        ),
    )
    return PipelineResult(
        original_code=CODE,
        scan=scan,
        final_fix=FixResult(
            patched_code="ok",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used=model,
            tokens_used=_mk(fix_tokens),
            cost_usd=(
                None
                if fix_tokens is None
                else telemetry.compute_cost_usd(model, *fix_tokens)
            ),
        ),
        final_validation=ValidationResult(
            eval_score=92,
            verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
            tokens_used=_mk(val_tokens),
            cost_usd=(
                None
                if val_tokens is None
                else telemetry.compute_cost_usd(model, *val_tokens)
            ),
        ),
        converged=True,
        routing_decisions={"scanner": model},
    )


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #

def test_cost_is_not_always_zero():
    """The defect, directly. A scan that made paid calls must not report free."""
    span = _Span()
    cost = api_router._compute_and_record_cost(_pipeline(), span)
    assert cost > 0.0, (
        "cost_usd computed as 0.0 for a pipeline that consumed 2652 tokens — "
        "the Cost card on the result page shows $0.00 for every scan"
    )


def test_cost_is_the_sum_of_what_the_agents_actually_spent():
    p = _pipeline()
    expected = (
        p.scan.cost_usd + p.final_fix.cost_usd + p.final_validation.cost_usd
    )
    assert api_router._compute_and_record_cost(p, _Span()) == pytest.approx(expected)


def test_a_clean_scan_costs_only_the_scanner():
    """The short-circuit path runs no Fixer and no Validator."""
    p = _pipeline(fix_tokens=None, val_tokens=None)
    p.final_validation = None
    cost = api_router._compute_and_record_cost(p, _Span())
    assert cost == pytest.approx(p.scan.cost_usd)


def test_cost_is_none_when_nothing_reported_usage():
    """`0.0` is a measurement — "this scan was free" — and it is false for a scan
    that made LLM calls the provider simply did not report usage for."""
    p = _pipeline(scan_tokens=None, fix_tokens=None, val_tokens=None)
    assert api_router._compute_and_record_cost(p, _Span()) is None


def test_the_span_carries_the_real_numbers():
    """These attributes go to SigNoz. They said 0 / 0 / 0 on every scan."""
    span = _Span()
    api_router._compute_and_record_cost(_pipeline(), span)

    assert span.attrs["llm.total_tokens"] == 900 + 512 + 600 + 300 + 200 + 140
    assert span.attrs["llm.cost_usd"] > 0.0
    assert span.attrs["llm.model_id"] == "llama-3.3-70b-versatile"
    assert span.attrs["llm.model_id"] != "__default__", (
        "the span reported the placeholder model id on every scan"
    )


def test_the_span_says_so_when_usage_is_unknown():
    span = _Span()
    api_router._compute_and_record_cost(
        _pipeline(scan_tokens=None, fix_tokens=None, val_tokens=None), span
    )
    assert span.attrs["llm.usage_reported"] is False


# --------------------------------------------------------------------------- #
# tokens_per_sec — a histogram that has never received an observation
# --------------------------------------------------------------------------- #

def test_tokens_per_sec_is_recorded(monkeypatch):
    """`telemetry.py` documents devguard.llm.tokens_per_sec as a real metric.

    The guard `if total_tokens and ...` never passed, because total_tokens was
    read from fields PipelineResult does not have, so the histogram was
    permanently empty.
    """
    observed: list = []

    class Hist:
        def record(self, v, attrs=None):
            observed.append((v, attrs))

    monkeypatch.setattr(telemetry, "TOKENS_PER_SEC", Hist())
    monkeypatch.setattr(telemetry, "REQUEST_LATENCY_MS", Hist())

    api_router._emit_request_metrics(_pipeline(), latency_s=2.0, cache_hit=False)

    rates = [v for v, a in observed if a is None]
    assert rates, "tokens_per_sec never received an observation"
    assert rates[0] == pytest.approx((900 + 512 + 600 + 300 + 200 + 140) / 2.0)


def test_tokens_per_sec_is_skipped_when_usage_is_unknown(monkeypatch):
    observed: list = []

    class Hist:
        def record(self, v, attrs=None):
            observed.append((v, attrs))

    monkeypatch.setattr(telemetry, "TOKENS_PER_SEC", Hist())
    monkeypatch.setattr(telemetry, "REQUEST_LATENCY_MS", Hist())

    api_router._emit_request_metrics(
        _pipeline(scan_tokens=None, fix_tokens=None, val_tokens=None),
        latency_s=2.0,
        cache_hit=False,
    )
    assert [v for v, a in observed if a is None] == []


def test_latency_is_still_recorded_with_the_cache_hit_label(monkeypatch):
    observed: list = []

    class Hist:
        def record(self, v, attrs=None):
            observed.append((v, attrs))

    monkeypatch.setattr(telemetry, "REQUEST_LATENCY_MS", Hist())
    monkeypatch.setattr(telemetry, "TOKENS_PER_SEC", None)

    api_router._emit_request_metrics(_pipeline(), latency_s=1.5, cache_hit=True)
    assert observed[0][0] == pytest.approx(1500.0)
    assert observed[0][1] == {"cache_hit": "True"}


# --------------------------------------------------------------------------- #
# A cache hit costs nothing THIS time — and must say so
# --------------------------------------------------------------------------- #

def test_a_cache_hit_reports_no_new_spend():
    """Newly reachable: the cache never hit before, so this path never ran.

    The cached PipelineResult carries the ORIGINAL run's tokens and cost. Billing
    them again to a request that made zero LLM calls would double-count spend on
    every repeat scan. The figures still belong in the response — they describe
    how the result was produced — but under a name that says so.
    """
    from backend.core.schemas import ScanRequest

    body = api_router._build_frontend_result(
        "s1", ScanRequest(code=CODE), _pipeline(), "a" * 32, 3.0, cost=0.0,
        cached_hit=True,
    )
    assert body["cost_usd"] == 0.0
    assert body["cached"] is True
    assert body["origin_cost_usd"] > 0.0
    assert body["origin_tokens_used"] == 2652


def test_a_fresh_scan_is_not_marked_cached():
    from backend.core.schemas import ScanRequest

    body = api_router._build_frontend_result(
        "s1", ScanRequest(code=CODE), _pipeline(), "a" * 32, 3.0, cost=0.0042,
    )
    assert body["cached"] is False
    assert body["cost_usd"] == 0.0042
    assert body["tokens_used"] == 2652


# --------------------------------------------------------------------------- #
# _extract_score works only via its fallback — pin that
# --------------------------------------------------------------------------- #

def test_the_score_comes_from_the_validation():
    assert api_router._extract_score(_pipeline()) == 92.0


def test_the_score_is_zero_when_no_validator_ran():
    p = _pipeline()
    p.final_validation = None
    assert api_router._extract_score(p) == 0.0
