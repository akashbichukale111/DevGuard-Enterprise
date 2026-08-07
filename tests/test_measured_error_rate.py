"""
Pre-Cog's error rate is measured, or it is absent — never invented.

The module forecasts circuit-breaker trips by extrapolating a current error
rate. That rate came from the SigNoz MCP client when reachable and from
`random.uniform(1.0, 8.0)` when not — and `SIGNOZ_MCP_URL` is unset in the
deployed backend, so the fallback was the branch production always took. Every
forecast on the public Nexus was a linear extrapolation of a random number,
rendered as a chart.

The process runs the pipeline, so it can measure how often the pipeline fails.
These tests pin that it does, that the measurement is real rather than
coincidentally plausible, and that a window with no attempts reports null with
a reason instead of falling back to a number.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core import god_mode_orchestrator as gmo
from backend.core.local_telemetry import (
    get_recent_error_rate_local,
    record_pipeline_outcome,
    reset_outcomes,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_outcomes():
    reset_outcomes()
    yield
    reset_outcomes()


@pytest.fixture()
def dead_mcp(monkeypatch):
    """No SigNoz, which is the deployed backend's permanent state."""
    class _Dead:
        async def get_error_rate_detailed(self):
            raise RuntimeError("no MCP server configured")

    monkeypatch.setattr(gmo, "get_mcp_client", lambda: _Dead())


# --------------------------------------------------------------------------- #
# The measurement itself
# --------------------------------------------------------------------------- #

def test_no_attempts_reports_unavailable_not_zero():
    """
    Zero would be a lie in the flattering direction: "no failures" and "no
    data" are different claims, and only one of them is true here.
    """
    r = get_recent_error_rate_local()
    assert r["available"] is False
    assert r["error_rate_pct"] is None
    assert r["reason"]
    assert r["samples"] == 0


def test_all_successes_is_a_real_zero():
    for _ in range(10):
        record_pipeline_outcome(ok=True)
    r = get_recent_error_rate_local()
    assert r["available"] is True
    assert r["error_rate_pct"] == 0.0
    assert r["samples"] == 10


def test_the_rate_is_arithmetic_not_approximation():
    for _ in range(7):
        record_pipeline_outcome(ok=True)
    for _ in range(3):
        record_pipeline_outcome(ok=False)
    r = get_recent_error_rate_local()
    assert r["error_rate_pct"] == 30.0
    assert r["failures"] == 3
    assert r["samples"] == 10


def test_all_failures_is_a_hundred_percent():
    for _ in range(4):
        record_pipeline_outcome(ok=False)
    assert get_recent_error_rate_local()["error_rate_pct"] == 100.0


def test_samples_outside_the_window_are_excluded():
    """A failure an hour ago must not drive a scale-out recommendation now."""
    old = time.time() - 3600
    for _ in range(5):
        record_pipeline_outcome(ok=False, ts=old)
    for _ in range(5):
        record_pipeline_outcome(ok=True)

    r = get_recent_error_rate_local(window_minutes=30)
    assert r["samples"] == 5
    assert r["error_rate_pct"] == 0.0


def test_a_window_wide_enough_sees_the_old_samples():
    """The converse, so the exclusion above is the window and not a bug."""
    old = time.time() - 3600
    for _ in range(5):
        record_pipeline_outcome(ok=False, ts=old)
    r = get_recent_error_rate_local(window_minutes=120)
    assert r["samples"] == 5
    assert r["error_rate_pct"] == 100.0


def test_tracking_is_bounded():
    """This must not become a memory leak in a long-running process."""
    from backend.core import local_telemetry

    for _ in range(local_telemetry._MAX_OUTCOMES + 500):
        record_pipeline_outcome(ok=True)
    assert len(local_telemetry._outcomes) <= local_telemetry._MAX_OUTCOMES


# --------------------------------------------------------------------------- #
# What Pre-Cog does with it
# --------------------------------------------------------------------------- #

def test_precog_uses_the_measured_rate_when_mcp_is_dead(dead_mcp):
    for _ in range(8):
        record_pipeline_outcome(ok=True)
    for _ in range(2):
        record_pipeline_outcome(ok=False)

    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] == 20.0
    assert result["data_source_detail"]["error_rate"] == "local_shadow"


def test_the_rate_is_not_random(dead_mcp):
    """
    The defect, directly. Ten consecutive runs over identical recorded
    outcomes must return an identical rate — `random.uniform(1.0, 8.0)` could
    not.
    """
    for _ in range(9):
        record_pipeline_outcome(ok=True)
    record_pipeline_outcome(ok=False)

    rates = [
        run(gmo.execute_precog_agent())["current_error_rate_pct"]
        for _ in range(10)
    ]
    assert len(set(rates)) == 1, f"the error rate is still being invented: {rates}"
    assert rates[0] == 10.0


def test_no_traffic_means_no_forecast(dead_mcp):
    """
    An extrapolation from an invented origin is an invented forecast, however
    careful the arithmetic. The panel already renders an empty series as
    "No forecast returned for this run".
    """
    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] is None
    assert result["error_rate_forecast"] == []
    assert result["projected_minutes_to_breaker_trip"] is None
    assert result["current_error_rate_unavailable_reason"]


def test_the_reasoning_explains_an_absent_forecast(dead_mcp):
    """A null on screen needs its reason attached, not left to the reader."""
    reasoning = run(gmo.execute_precog_agent())["reasoning"]
    assert "No error-rate forecast" in reasoning
    assert "no pipeline attempts" in reasoning


def test_a_measured_rate_produces_a_forecast(dead_mcp):
    for _ in range(10):
        record_pipeline_outcome(ok=False)

    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] == 100.0
    assert len(result["error_rate_forecast"]) > 0
    # Already past the breaker threshold, so the trip is immediate.
    assert result["projected_minutes_to_breaker_trip"] == 0
    assert result["autoscale_recommendation"] == "scale_out"


def test_a_healthy_measured_rate_recommends_holding(dead_mcp):
    for _ in range(50):
        record_pipeline_outcome(ok=True)

    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] == 0.0
    assert result["autoscale_recommendation"] == "hold"
    assert result["projected_minutes_to_breaker_trip"] is None


def test_live_telemetry_still_wins_over_the_local_shadow(monkeypatch):
    """The MCP path is the better source and must stay first in the ladder."""
    class _Live:
        async def get_error_rate_detailed(self):
            class _R:
                available = True
                error_rate_pct = 4.25
            return _R()

    monkeypatch.setattr(gmo, "get_mcp_client", lambda: _Live())
    for _ in range(10):
        record_pipeline_outcome(ok=False)  # would read as 100% locally

    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] == 4.25
    assert result["data_source_detail"]["error_rate"] == "live"


# --------------------------------------------------------------------------- #
# Provenance aggregation
# --------------------------------------------------------------------------- #

def test_an_absent_axis_never_badges_the_payload_above_partial(dead_mcp):
    result = run(gmo.execute_precog_agent())
    assert result["data_source"] in ("partial", "synthetic")
    assert result["data_source"] != "live"


def test_an_invented_axis_outranks_an_absent_one_in_the_badge():
    """
    A payload holding an invented leak rate and an absent error rate must badge
    "synthetic" — the invention is the thing a reader needs warning about, and
    tying on rank must not let list order decide the label.
    """
    assert gmo._aggregate_source("unavailable", "synthetic") == "synthetic"
    assert gmo._aggregate_source("synthetic", "unavailable") == "synthetic"


def test_wholly_absent_is_reported_as_absent_not_invented():
    assert gmo._aggregate_source("unavailable", "unavailable") == "unavailable"


# --------------------------------------------------------------------------- #
# The wiring — without this the measurement above is a module nothing calls
# --------------------------------------------------------------------------- #

def test_a_failed_scan_is_recorded_as_a_failure(monkeypatch):
    """
    Drives the real endpoint. Without a reachable model /scan fails, which is
    exactly the outcome the error rate exists to count — and it is the state
    the deployed backend is in whenever its key is missing or the provider is
    down.
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from fastapi.testclient import TestClient
    from backend.main import app

    reset_outcomes()
    with TestClient(app) as c:
        r = c.post("/scan", json={"code": "import os\n", "language": "python"})

    assert r.status_code >= 500, "expected the pipeline to fail without a model"
    measured = get_recent_error_rate_local()
    assert measured["available"] is True, (
        "the scan endpoint is not recording outcomes — the error rate would "
        "silently stay unavailable in production"
    )
    assert measured["samples"] == 1
    assert measured["error_rate_pct"] == 100.0


def test_rejected_input_is_not_counted_as_a_pipeline_failure(monkeypatch):
    """
    A 422 is the caller's mistake, not the pipeline's. Counting it would let
    anyone drive the error rate — and the autoscale recommendation derived from
    it — by posting malformed bodies.
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from fastapi.testclient import TestClient
    from backend.main import app

    reset_outcomes()
    with TestClient(app) as c:
        r = c.post("/scan", json={"not_code": True})

    assert r.status_code == 422
    assert get_recent_error_rate_local()["available"] is False


def test_the_executive_digest_does_not_report_absence_as_safety(dead_mcp):
    """
    A null breaker ETA has two causes: nothing trips inside the horizon (a
    measured all-clear) or there is no error rate at all (no evidence). The
    digest used to render both as "no imminent risk", which turns missing data
    into a claim of safety.
    """
    reset_outcomes()
    digest = run(gmo.generate_executive_summary())["slack_message"]
    assert "no imminent risk" not in digest
    assert "no error-rate data" in digest


def test_a_measured_all_clear_still_reads_as_no_imminent_risk(dead_mcp):
    """The converse — otherwise the fix above would just suppress the phrase."""
    for _ in range(50):
        record_pipeline_outcome(ok=True)
    digest = run(gmo.generate_executive_summary())["slack_message"]
    assert "no imminent risk" in digest
