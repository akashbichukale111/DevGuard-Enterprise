"""
The in-process cost shadow that drives the budget decision.

`local_telemetry.py` records LLM spend in-process, and
`self_observer.adaptive_select_model` compares its 30-minute total against
`COST_BUDGET_USD_PER_30MIN` to decide whether to downgrade the Fixer's model. So
this module's numbers are not decoration — they gate a routing decision.

THE DEFECTS THESE TESTS EXIST FOR
---------------------------------
1. **It approximated when the real figure was available.** Cost was estimated as
   `(len(text_in) + len(text_out)) / 4` tokens at a flat per-model price. The
   module's own docstring said: *"Swap in real `usage.total_tokens` from the Groq
   response later for exact figures."* That became possible when `_call_cost`
   landed — the provider's prompt/completion split is right there in the same
   function that calls `record_llm_call`.

   Measured divergence on realistic agent calls:

       scanner: 6KB in, 1KB out      heuristic $0.001050  real $0.001082   -3.0%
       fixer:   6KB in, 6KB out      heuristic $0.001800  real $0.002070  -13.0%
       validator: 12KB in, 500B out  heuristic $0.001875  real $0.001869   +0.3%
       TOTAL over one pipeline run   heuristic $0.004770  real $0.005070   -5.9%

   It **under**-reports, so the budget gate fires later than it should and spend
   overshoots the ceiling before conservation mode engages. Wrong direction for a
   cost guard. The error is not a constant offset either — it tracks the
   prompt/completion ratio, because the heuristic uses one flat price where real
   pricing splits prompt from completion (0.59 vs 0.79 per 1M).

2. **Two independent price tables for the same models.**
   `telemetry.MODEL_PRICING_USD_PER_1M` (per 1M, split) and
   `local_telemetry.PRICE_PER_1K_TOKENS_USD` (per 1K, flat). Update one and the
   other silently diverges — and the stale one is what gates the budget.
"""

from __future__ import annotations

import pytest

from backend.core import local_telemetry as shadow
from backend.core.telemetry import MODEL_PRICING_USD_PER_1M, compute_cost_usd

M70 = "llama-3.3-70b-versatile"
M8 = "llama-3.1-8b-instant"


@pytest.fixture(autouse=True)
def clean():
    shadow.reset()
    yield
    shadow.reset()


# --------------------------------------------------------------------------- #
# The exact figure is used when it is known
# --------------------------------------------------------------------------- #

def test_an_exact_cost_is_recorded_verbatim():
    """The defect, directly: a known figure must not be re-estimated."""
    exact = compute_cost_usd(M70, 1500, 250)
    recorded = shadow.record_llm_call(M70, "x" * 6000, "y" * 1000, cost_usd=exact)
    assert recorded == exact


def test_the_trend_totals_exact_costs():
    a = compute_cost_usd(M70, 1500, 250)
    b = compute_cost_usd(M70, 1500, 1500)
    shadow.record_llm_call(M70, "x", "y", cost_usd=a)
    shadow.record_llm_call(M70, "x", "y", cost_usd=b)

    trend = shadow.get_recent_cost_trend_local()
    assert trend["total_cost_usd"] == pytest.approx(a + b)
    assert trend["request_count"] == 2
    assert trend["estimated_samples"] == 0, (
        "an exact figure was counted as an estimate"
    )


def test_the_trend_says_when_it_is_estimating():
    """A budget decision made on approximations should be able to say so."""
    shadow.record_llm_call(M70, "x" * 6000, "y" * 1000)  # no exact cost
    trend = shadow.get_recent_cost_trend_local()
    assert trend["estimated_samples"] == 1
    assert trend["exact"] is False


def test_a_fully_exact_window_reports_exact():
    shadow.record_llm_call(M70, "x", "y", cost_usd=0.001)
    shadow.record_llm_call(M8, "x", "y", cost_usd=0.0002)
    assert shadow.get_recent_cost_trend_local()["exact"] is True


def test_a_mixed_window_is_not_reported_as_exact():
    shadow.record_llm_call(M70, "x", "y", cost_usd=0.001)
    shadow.record_llm_call(M70, "x" * 100, "y" * 100)
    trend = shadow.get_recent_cost_trend_local()
    assert trend["exact"] is False
    assert trend["estimated_samples"] == 1


# --------------------------------------------------------------------------- #
# The estimate is still there, and now agrees with the real price table
# --------------------------------------------------------------------------- #

def test_the_estimate_uses_the_same_price_table_as_the_real_calculation():
    """Two tables for the same models is a drift bug waiting to happen: update
    one and the other silently disagrees, and the stale one gates the budget."""
    for model in (M70, M8):
        # 4000 chars in + 4000 out -> 1000 + 1000 tokens under the 4-chars
        # heuristic, so the estimate must equal the real calc at those counts.
        est = shadow.estimate_cost_usd(model, "x" * 4000, "y" * 4000)
        exact = compute_cost_usd(model, 1000, 1000)
        assert est == pytest.approx(exact, rel=1e-9), (
            f"the estimate for {model} does not agree with compute_cost_usd"
        )


def test_the_estimate_splits_prompt_and_completion_pricing():
    """A flat price cannot be right for both. Completion costs more, so a
    completion-heavy call must estimate higher than a prompt-heavy one of the
    same total size."""
    prompt_heavy = shadow.estimate_cost_usd(M70, "x" * 8000, "y" * 800)
    completion_heavy = shadow.estimate_cost_usd(M70, "x" * 800, "y" * 8000)
    assert completion_heavy > prompt_heavy


def test_an_unknown_model_is_priced_conservatively_high():
    """telemetry.py prices unknown models high on purpose, "so unknown spend is
    never *under*-reported (bias toward alerting)". The shadow must inherit that
    bias, not undercut it — it is the one gating the budget."""
    unknown = shadow.estimate_cost_usd("some-new-model", "x" * 4000, "y" * 4000)
    known = shadow.estimate_cost_usd(M70, "x" * 4000, "y" * 4000)
    assert unknown > known
    assert unknown == pytest.approx(compute_cost_usd("__default__", 1000, 1000))


def test_the_shadow_has_no_second_price_table():
    """Regression guard on the source: one table, one place to update."""
    assert not hasattr(shadow, "PRICE_PER_1K_TOKENS_USD"), (
        "a second price table reappeared in local_telemetry"
    )


# --------------------------------------------------------------------------- #
# Windowing and bookkeeping
# --------------------------------------------------------------------------- #

def test_samples_outside_the_window_are_excluded():
    import time

    shadow.record_llm_call(M70, "x", "y", cost_usd=1.0, ts=time.time() - 3600)
    shadow.record_llm_call(M70, "x", "y", cost_usd=2.0)

    trend = shadow.get_recent_cost_trend_local(window_minutes=30)
    assert trend["request_count"] == 1
    assert trend["total_cost_usd"] == pytest.approx(2.0)


def test_cost_is_broken_down_by_model():
    shadow.record_llm_call(M70, "x", "y", cost_usd=0.003)
    shadow.record_llm_call(M8, "x", "y", cost_usd=0.001)
    shadow.record_llm_call(M8, "x", "y", cost_usd=0.001)

    by_model = shadow.get_recent_cost_trend_local()["cost_by_model"]
    assert by_model[M70] == pytest.approx(0.003)
    assert by_model[M8] == pytest.approx(0.002)


def test_an_empty_window_reports_zero_not_an_error():
    trend = shadow.get_recent_cost_trend_local()
    assert trend["total_cost_usd"] == 0.0
    assert trend["request_count"] == 0
    assert trend["avg_cost_per_request_usd"] == 0.0
    # Nothing was estimated, so nothing is inexact.
    assert trend["exact"] is True


def test_the_sample_buffer_is_bounded():
    """It is a fixed-size deque; a long-running process must not grow here."""
    for i in range(shadow._samples.maxlen + 500):
        shadow.record_llm_call(M70, "x", "y", cost_usd=1e-6)
    assert len(shadow._samples) == shadow._samples.maxlen


# --------------------------------------------------------------------------- #
# The budget decision this feeds
# --------------------------------------------------------------------------- #

def test_under_reporting_would_delay_the_budget_gate():
    """Why the accuracy matters, stated as the decision it drives.

    The heuristic under-reported, so the 30-minute total crossed the ceiling
    later than it should have and spend overshot before conservation mode
    engaged. With exact costs, the same calls cross the same ceiling on time.
    """
    from backend.core import self_observer

    ceiling = 0.005
    calls = [(1500, 250), (1500, 1500), (3000, 125)]
    exact_total = sum(compute_cost_usd(M70, p, c) for p, c in calls)
    assert exact_total > ceiling, "pick a ceiling the exact total actually crosses"

    for p, c in calls:
        shadow.record_llm_call(M70, "x", "y", cost_usd=compute_cost_usd(M70, p, c))

    trend = shadow.get_recent_cost_trend_local()
    assert trend["total_cost_usd"] > ceiling
    assert trend["exact"] is True
