"""
Circuit breaker state-machine tests (Checks: "pipeline tests: converges ·
exhausts retries · circuit breaker opens · degrades with telemetry
unavailable").

The breaker is the component standing between a flapping LLM provider and a
runaway bill, and README calls it out by name. These tests drive the real state
machine — CLOSED -> OPEN -> HALF_OPEN -> CLOSED — with a fake async callable, so
no API key and no network are involved.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ai_agent import AgentExecutionError
from backend.core.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


@pytest.fixture
def breaker():
    # Small numbers keep the tests fast and the intent obvious.
    return CircuitBreaker(name="test_breaker", fail_max=3, reset_timeout=0.3)


async def _ok():
    return "ok"


async def _boom():
    raise AgentExecutionError("fixer_agent", "upstream exploded")


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# CLOSED
# --------------------------------------------------------------------------- #

def test_starts_closed(breaker):
    assert breaker.state is CircuitState.CLOSED


def test_successful_calls_pass_through_and_keep_it_closed(breaker):
    for _ in range(10):
        assert run(breaker.call(_ok)) == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_failures_below_the_threshold_do_not_open_it(breaker):
    for _ in range(2):  # fail_max is 3
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    assert breaker.state is CircuitState.CLOSED


def test_a_success_resets_the_failure_count(breaker):
    """Two failures then a success must not leave the breaker one away from
    opening — otherwise intermittent errors trip it over a long enough window."""
    for _ in range(2):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    run(breaker.call(_ok))

    # A further two failures should still be under the threshold.
    for _ in range(2):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    assert breaker.state is CircuitState.CLOSED


# --------------------------------------------------------------------------- #
# OPEN
# --------------------------------------------------------------------------- #

def test_opens_once_the_threshold_is_reached(breaker):
    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    assert breaker.state is CircuitState.OPEN


def test_open_breaker_suppresses_the_primary_path(breaker):
    """The point of the breaker: stop calling a provider that is failing."""
    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))

    called = False

    async def _tracked():
        nonlocal called
        called = True
        return "should not happen"

    with pytest.raises(CircuitOpenError):
        run(breaker.call(_tracked))
    assert called is False, "breaker was OPEN but still invoked the callable"


def test_circuit_open_error_names_the_breaker(breaker):
    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    with pytest.raises(CircuitOpenError, match="test_breaker"):
        run(breaker.call(_ok))


# --------------------------------------------------------------------------- #
# HALF_OPEN — recovery
# --------------------------------------------------------------------------- #

def test_moves_to_half_open_after_the_cooldown(breaker):
    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    assert breaker.state is CircuitState.OPEN

    import time

    time.sleep(0.35)  # reset_timeout is 0.3
    # The transition happens lazily, on the next call attempt.
    assert run(breaker.call(_ok)) == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_a_failed_probe_slams_it_back_open(breaker):
    import time

    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    time.sleep(0.35)

    with pytest.raises(AgentExecutionError):
        run(breaker.call(_boom))  # the half-open probe fails
    assert breaker.state is CircuitState.OPEN

    # And the cooldown clock restarts — it must not be immediately probeable.
    with pytest.raises(CircuitOpenError):
        run(breaker.call(_ok))


def test_recovery_restores_normal_service(breaker):
    """Full round trip: healthy -> broken -> recovered."""
    import time

    assert run(breaker.call(_ok)) == "ok"
    for _ in range(3):
        with pytest.raises(AgentExecutionError):
            run(breaker.call(_boom))
    assert breaker.state is CircuitState.OPEN

    time.sleep(0.35)
    assert run(breaker.call(_ok)) == "ok"
    assert breaker.state is CircuitState.CLOSED
    for _ in range(5):
        assert run(breaker.call(_ok)) == "ok"


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #

def test_unexpected_exception_types_propagate_without_tripping_the_breaker():
    """FAILURE_EXCEPTIONS is an explicit allowlist. A programming error (e.g.
    ValueError) is not an upstream outage and must not consume the budget that
    protects against one."""
    b = CircuitBreaker(name="strict", fail_max=2, reset_timeout=5)

    async def _bug():
        raise ValueError("a bug in our own code")

    for _ in range(4):
        with pytest.raises(ValueError):
            run(b.call(_bug))
    assert b.state is CircuitState.CLOSED


def test_breakers_are_independent():
    """One provider failing must not suppress another."""
    a = CircuitBreaker(name="a", fail_max=2, reset_timeout=5)
    b = CircuitBreaker(name="b", fail_max=2, reset_timeout=5)

    for _ in range(2):
        with pytest.raises(AgentExecutionError):
            run(a.call(_boom))

    assert a.state is CircuitState.OPEN
    assert b.state is CircuitState.CLOSED
    assert run(b.call(_ok)) == "ok"
