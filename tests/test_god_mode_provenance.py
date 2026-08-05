"""
`data_source` must describe the whole payload, not its best part.

`god_mode_orchestrator.py`'s own docstring:

    "Every response includes a `data_source` field: `"live"` when it was
     produced from real telemetry/pipeline output..."

SECURITY.md, "What is implemented today":

    "Honest provenance. Every god-mode response carries a `data_source` of
     `live`, `local_shadow`, `synthetic` or `partial`, and the UI renders it. An
     in-process estimate is never presented as retrieved telemetry."

That contract holds only if one `data_source` value is true of everything in the
response. In `execute_precog_agent` it was not.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
`execute_precog_agent` flips `data_source` to `"live"` when the **error rate**
comes back from MCP. But the memory axis of the same response is unconditionally
invented:

    starting_rss_mb    = round(random.uniform(160.0, 260.0), 1)
    leak_rate_mb_per_min = round(random.uniform(2.0, 14.0), 2)
    minutes_to_oom     = (oom_ceiling_mb - starting_rss_mb) / leak_rate_mb_per_min

So a payload badged `"live"` carried a fabricated current RSS, a fabricated leak
rate, and an OOM countdown derived from both — and `frontend/components/nexus/
PreCogPanel.tsx` renders all three (`starting_rss_mb`, `leak_rate_mb_per_min`,
`projected_minutes_to_oom`).

The error-rate half being real does not make the memory half real. A single flag
that reports the most favourable component is exactly the mislabel pattern
already fixed twice during hardening — the MCP cost fallback reporting "live", and the
executive roll-up deriving provenance from the absence of errors.

Two fixes here:

* The current RSS is **measurable** — this process's own resident set size, from
  the stdlib. There is no reason to invent it.
* The leak *rate* is not measurable from one sample, so it stays a scenario
  parameter and is labelled as one. `data_source` now reports the honest
  aggregate, and a per-axis breakdown says which half came from where.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import god_mode_orchestrator as gmo


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def live_error_rate(monkeypatch):
    """MCP answering the error-rate query — the condition that set "live"."""
    class _Err:
        available = True
        error_rate_pct = 4.25

    class _Client:
        async def get_error_rate_detailed(self):
            return _Err()

    monkeypatch.setattr(gmo, "get_mcp_client", lambda: _Client())


@pytest.fixture
def dead_mcp(monkeypatch):
    class _Client:
        async def get_error_rate_detailed(self):
            raise RuntimeError("no MCP server configured")

    monkeypatch.setattr(gmo, "get_mcp_client", lambda: _Client())


# --------------------------------------------------------------------------- #
# The label must not overstate
# --------------------------------------------------------------------------- #

def test_a_live_error_rate_does_not_make_the_whole_payload_live(live_error_rate):
    """The defect, directly.

    The memory axis is a scenario projection either way, so the payload as a
    whole is at best partial — never "live".
    """
    result = run(gmo.execute_precog_agent())
    assert result["data_source"] != "live", (
        "a payload containing an invented leak rate and OOM countdown was "
        "badged 'live' because one of its two axes came from telemetry"
    )
    assert result["data_source"] == "partial"


def test_the_provenance_is_broken_down_per_axis(live_error_rate):
    """One flag cannot describe two independently-sourced halves."""
    src = run(gmo.execute_precog_agent())["data_source_detail"]
    assert src["error_rate"] == "live"
    assert src["memory"] in ("measured_current_synthetic_rate", "synthetic")


def test_with_no_telemetry_the_error_axis_is_synthetic(dead_mcp):
    """No MCP means the error rate is invented — but the RSS is still measured.

    This test originally asserted `data_source == "synthetic"`. That was written
    before the RSS measurement existed and is wrong now: with a real current RSS
    in the payload, "synthetic" would *understate* it. "partial" is the accurate
    aggregate, and the detail says which half is which. The genuinely
    all-synthetic case is covered by
    `test_an_rss_read_failure_degrades_to_synthetic_rather_than_raising`.
    """
    result = run(gmo.execute_precog_agent())
    assert result["data_source_detail"]["error_rate"] == "synthetic"
    assert result["data_source"] == "partial"


# --------------------------------------------------------------------------- #
# The current RSS is measurable, so it must be measured
# --------------------------------------------------------------------------- #

def test_the_current_rss_is_a_real_measurement(dead_mcp):
    """`random.uniform(160, 260)` for this process's own memory is indefensible
    when the real number is one stdlib call away.

    Ten consecutive reads, requiring a tight spread. Two draws from
    `uniform(160, 260)` can land close together by luck — ten cannot stay inside
    a 5 MB band, while ten reads of a real RSS trivially do.
    """
    reads = [
        run(gmo.execute_precog_agent())["memory_forecast"]["starting_rss_mb"]
        for _ in range(10)
    ]
    spread = max(reads) - min(reads)
    assert spread < 5.0, (
        f"ten consecutive readings of this process's RSS spanned {spread:.1f} MB "
        f"({min(reads)} - {max(reads)}) — the value is still being invented"
    )
    assert all(r > 0 for r in reads)


def test_the_measured_rss_matches_the_process(dead_mcp):
    measured = gmo._current_rss_mb()
    reported = run(gmo.execute_precog_agent())["memory_forecast"]["starting_rss_mb"]
    assert measured is not None
    assert reported == pytest.approx(measured, rel=0.25)


def test_the_memory_axis_says_the_rate_is_a_scenario(dead_mcp):
    """The leak rate cannot be measured from one sample, so it must not pretend
    to be. Being explicit is what keeps the OOM countdown honest."""
    mem = run(gmo.execute_precog_agent())["memory_forecast"]
    assert mem["rate_source"] == "synthetic_scenario"
    assert mem["current_rss_source"] == "measured"


def test_the_oom_projection_is_derived_from_the_two_and_labelled(dead_mcp):
    mem = run(gmo.execute_precog_agent())["memory_forecast"]
    expected = (mem["oom_ceiling_mb"] - mem["starting_rss_mb"]) / mem["leak_rate_mb_per_min"]
    assert mem["projected_minutes_to_oom"] == pytest.approx(round(expected, 1))
    assert mem["projection_source"] == "derived_from_synthetic_rate"


# --------------------------------------------------------------------------- #
# Nothing else regressed
# --------------------------------------------------------------------------- #

def test_the_error_rate_forecast_still_has_its_shape(live_error_rate):
    result = run(gmo.execute_precog_agent())
    assert result["current_error_rate_pct"] == 4.25
    forecast = result["error_rate_forecast"]
    assert [p["minute"] for p in forecast] == [0, 3, 6, 9, 12, 15]
    # Monotonically rising linear projection.
    rates = [p["projected_error_rate_pct"] for p in forecast]
    assert rates == sorted(rates)
    assert rates[0] == 4.25


def test_the_module_and_run_id_are_still_reported(dead_mcp):
    result = run(gmo.execute_precog_agent())
    assert result["module"] == "precog_ops"
    assert result["run_id"].startswith("precog")


def test_an_rss_read_failure_degrades_to_synthetic_rather_than_raising(
    dead_mcp, monkeypatch
):
    """Fail-safe: a god-mode panel must never 500 because /proc is unreadable."""
    monkeypatch.setattr(gmo, "_current_rss_mb", lambda: None)
    result = run(gmo.execute_precog_agent())
    mem = result["memory_forecast"]
    assert mem["starting_rss_mb"] > 0
    assert mem["current_rss_source"] == "synthetic"
    assert result["data_source"] == "synthetic"
