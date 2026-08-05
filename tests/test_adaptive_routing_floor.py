"""
The critical-severity safety floor.

This is the load-bearing safety property of the self-observation layer, and the
one the project advertises most loudly. README:

    "Downgrades model tier under cost pressure — **never for `critical`
     severity**, a hard-coded safety floor"

DEMO_SCRIPT.md, said to camera:

    "Cost pressure can NEVER downgrade a critical fix."

The consequence of it failing is not a cosmetic one. A critical SQL injection
would be handed to `llama-3.1-8b-instant` instead of the 70B model, silently, to
save money — the worst possible place to economise, and invisible in the result
because the response would just report the cheap model as the routing decision.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
The floor was a bare string comparison:

    if severity == SEVERITY_CRITICAL:      # "critical"
        return base_model, None

`select_model` two functions away is deliberately defensive about exactly this,
and says why in its own docstring: it coerces through the `Severity` enum so an
unknown value "fails loudly rather than silently defaulting to the cheap model —
silently under-provisioning a critical scan is the exact failure mode we must
never have."

The floor had no such coercion, so it FAILED OPEN on any format variance:
`"CRITICAL"`, `" critical"`, or `"Severity.CRITICAL"` all missed the branch and
fell through to the cost logic, which downgrades. That last form is not
hypothetical — `backend/api/router.py` normalises severity with
`str(...).replace("Severity.", "").lower()`, which exists precisely because
`"Severity.CRITICAL"` strings circulate in this codebase.

A safety floor that depends on its caller passing exactly the right casing is
not a floor. These tests pin it to the enum instead.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import self_observer
from backend.core.ai_agent import MODEL_CHEAP, MODEL_STRONG
from backend.core.schemas import Severity


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def under_cost_pressure(monkeypatch):
    """Maximum downward pressure: conservation mode ON and the budget blown.

    Both downgrade paths are live, so anything that is not held up by the floor
    will come back as the cheap model.
    """
    monkeypatch.setattr(self_observer, "is_conservation_mode", lambda: True)
    return monkeypatch


@pytest.fixture
def over_budget(monkeypatch):
    """Conservation mode off, but the 30-minute spend is over the ceiling."""
    monkeypatch.setattr(self_observer, "is_conservation_mode", lambda: False)
    monkeypatch.setattr(self_observer, "COST_BUDGET_USD_PER_30MIN", 1.0)

    class _Trend:
        available = True
        total_cost_usd = 999.0
        source = "local_shadow"

    class _Client:
        async def get_recent_cost_trend(self):
            return _Trend()

    monkeypatch.setattr(self_observer, "get_mcp_client", lambda: _Client())
    return monkeypatch


# --------------------------------------------------------------------------- #
# The floor holds regardless of how the severity is spelled
# --------------------------------------------------------------------------- #

CRITICAL_SPELLINGS = [
    "critical",            # the canonical form
    "CRITICAL",            # upper-cased somewhere upstream
    "Critical",
    " critical ",          # whitespace from a header or query param
    "Severity.CRITICAL",   # str() of the enum — router.py strips this prefix,
    "Severity.critical",   # which proves the form is real and circulating
]


@pytest.mark.parametrize("spelling", CRITICAL_SPELLINGS)
def test_conservation_mode_never_downgrades_critical(under_cost_pressure, spelling):
    model, reason = run(self_observer.adaptive_select_model(spelling, MODEL_STRONG))
    assert model == MODEL_STRONG, (
        f"critical severity spelled {spelling!r} was DOWNGRADED to {model} under "
        "conservation mode — the safety floor failed open"
    )
    assert reason is None


@pytest.mark.parametrize("spelling", CRITICAL_SPELLINGS)
def test_cost_budget_never_downgrades_critical(over_budget, spelling):
    model, reason = run(self_observer.adaptive_select_model(spelling, MODEL_STRONG))
    assert model == MODEL_STRONG, (
        f"critical severity spelled {spelling!r} was DOWNGRADED to {model} with "
        "the cost budget exceeded — the safety floor failed open"
    )
    assert reason is None


def test_the_enum_value_itself_is_accepted(under_cost_pressure):
    """Callers reasonably pass the enum rather than remembering to call .value."""
    model, reason = run(
        self_observer.adaptive_select_model(Severity.CRITICAL, MODEL_STRONG)
    )
    assert model == MODEL_STRONG
    assert reason is None


# --------------------------------------------------------------------------- #
# An unrecognised severity must fail SAFE, not cheap
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("garbage", ["", "urgent", "sev1", "p0", None, 3])
def test_an_unknown_severity_is_not_downgraded(under_cost_pressure, garbage):
    """The direction of the failure is the whole point.

    If a new severity name, a typo, or a None reaches here, the safe outcome is
    "keep whatever model severity-based routing already chose". Downgrading an
    input we do not understand is how a critical scan gets under-provisioned
    without anyone noticing — `select_model`'s docstring names this exact failure
    mode as the one that must never happen.
    """
    model, reason = run(self_observer.adaptive_select_model(garbage, MODEL_STRONG))
    assert model == MODEL_STRONG, (
        f"unrecognised severity {garbage!r} was downgraded to {model}; an input "
        "this layer cannot interpret must never cost a scan its model tier"
    )


def test_an_unknown_severity_is_reported_not_silent(under_cost_pressure, caplog):
    """Failing safe silently hides a real integration bug. Say something."""
    import logging

    with caplog.at_level(logging.WARNING, logger="backend.core.self_observer"):
        run(self_observer.adaptive_select_model("sev1", MODEL_STRONG))

    assert any("sev1" in r.getMessage() for r in caplog.records), (
        "an uninterpretable severity produced no warning"
    )


# --------------------------------------------------------------------------- #
# Non-critical severities MUST still be downgradable — the floor is not a wall
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("severity", ["low", "medium", "high", "HIGH", "Severity.HIGH"])
def test_conservation_mode_downgrades_everything_below_critical(
    under_cost_pressure, severity
):
    """Guard against "fixing" the floor by never downgrading anything.

    That would silently delete the cost-control feature while making every test
    above pass.
    """
    model, reason = run(self_observer.adaptive_select_model(severity, MODEL_STRONG))
    assert model == MODEL_CHEAP
    assert reason == "conservation_mode_active"


@pytest.mark.parametrize("severity", ["low", "medium", "high"])
def test_cost_budget_downgrades_everything_below_critical(over_budget, severity):
    model, reason = run(self_observer.adaptive_select_model(severity, MODEL_STRONG))
    assert model == MODEL_CHEAP
    assert reason is not None
    assert "cost_budget_exceeded" in reason


def test_within_budget_nothing_is_downgraded(monkeypatch):
    monkeypatch.setattr(self_observer, "is_conservation_mode", lambda: False)
    monkeypatch.setattr(self_observer, "COST_BUDGET_USD_PER_30MIN", 100.0)

    class _Trend:
        available = True
        total_cost_usd = 0.01
        source = "local_shadow"

    class _Client:
        async def get_recent_cost_trend(self):
            return _Trend()

    monkeypatch.setattr(self_observer, "get_mcp_client", lambda: _Client())
    model, reason = run(self_observer.adaptive_select_model("high", MODEL_STRONG))
    assert model == MODEL_STRONG
    assert reason is None


def test_unavailable_telemetry_does_not_downgrade(monkeypatch):
    """Fail-safe: no telemetry means no evidence of cost pressure, so no action."""
    monkeypatch.setattr(self_observer, "is_conservation_mode", lambda: False)

    class _Trend:
        available = False
        total_cost_usd = 0.0
        source = "local_shadow"

    class _Client:
        async def get_recent_cost_trend(self):
            return _Trend()

    monkeypatch.setattr(self_observer, "get_mcp_client", lambda: _Client())
    model, reason = run(self_observer.adaptive_select_model("high", MODEL_STRONG))
    assert model == MODEL_STRONG
    assert reason is None


# --------------------------------------------------------------------------- #
# The floor as the pipeline actually reaches it
# --------------------------------------------------------------------------- #

def test_the_pipeline_passes_a_form_the_floor_accepts(under_cost_pressure):
    """Integration guard on the call-site contract.

    `run_pipeline` calls `_select_model_adaptive(_max_severity(vulns).value)`. If
    anyone changes that expression — to `str(sev)`, say, which really does yield
    "Severity.CRITICAL" for this `str`-subclassing enum — the floor must still
    hold. This asserts the two ends agree rather than trusting that they do.

    Takes `under_cost_pressure` deliberately: without it this test passes for the
    wrong reason, because an unconfigured MCP client reports no cost trend and
    nothing would be downgraded regardless of the floor.
    """
    from backend.core import ai_agent

    sev = ai_agent._max_severity(
        [
            _v(Severity.LOW),
            _v(Severity.CRITICAL),
            _v(Severity.MEDIUM),
        ]
    )
    assert sev is Severity.CRITICAL
    for form in (sev, sev.value, str(sev)):
        model, reason = run(self_observer.adaptive_select_model(form, MODEL_STRONG))
        assert model == MODEL_STRONG, f"floor missed the form {form!r}"


def _v(severity: Severity):
    from backend.core.schemas import Vulnerability

    return Vulnerability(
        cwe_id="CWE-89",
        severity=severity,
        line_number=1,
        explanation="Concatenated user input reaches a SQL execute call.",
        confidence_score=0.9,
    )
