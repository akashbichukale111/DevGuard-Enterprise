"""
backend/core/resilience.py
==========================
Circuit breaker + graceful degradation for the LLM-calling functions.

SRE DESIGN NOTES — BLAST RADIUS CONTAINMENT
-------------------------------------------
The Groq API is a hard external dependency. When an upstream LLM provider has a
bad hour (rate limits, 5xx storms, latency cliffs), the naive failure mode is:
every /scan request hangs for its full timeout, worker threads pile up, the
event loop saturates, and a *provider* incident becomes an *us* incident. That
is the exact blast-radius amplification a circuit breaker exists to prevent.

STATE MACHINE (classic 3-state breaker):
  CLOSED     -> normal. Count consecutive failures.
  OPEN       -> after N consecutive failures, stop calling primary. Fail fast /
                route to fallback. Stay open for `reset_timeout` (cooldown).
  HALF_OPEN  -> after cooldown, allow ONE trial call. Success -> CLOSED.
                Failure -> back to OPEN (restart cooldown).

TUNING RATIONALE (documented for postmortem review):
  fail_max = 5        -> Five *consecutive* failures. Low enough to trip within
                         a couple seconds of a real outage; high enough that a
                         single transient blip doesn't flap the breaker.
  reset_timeout = 30s -> Cooldown. LLM provider incidents rarely resolve in <30s;
                         probing more aggressively just adds load to an already
                         struggling upstream (and burns our rate limit). 30s is
                         the sweet spot between "recover fast" and "don't DDoS
                         the recovery."
  FALLBACK strategy   -> While OPEN we don't just fail — we degrade to a smaller,
                         cheaper, usually-more-available model (llama-3.1-8b-instant).
                         A degraded-but-live answer beats a 503 for a code scan.

Every state transition is emitted as a span event AND a counter metric, so in
SigNoz you can overlay "breaker opened" markers on the latency graph and
instantly correlate a latency spike with the upstream that caused it.

SELF-OBSERVATION LAYER (added):
  On every CLOSED -> OPEN (or HALF_OPEN -> OPEN) transition, the breaker now
  fires off an async, best-effort request to `PostmortemAgent` asking it to
  narrate "what just happened" in plain language, and attaches that narration
  to the same span as a `circuit_breaker.postmortem` event. This turns a raw
  "breaker opened" telemetry blip into something a human (or another agent)
  can read without correlating logs by hand. This generation is intentionally
  OFF the synchronous state-machine path — see the `# SELF-OBSERVATION:`
  comments below for exactly where and why.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from backend.core.ai_agent import AgentExecutionError, run_pipeline
# `run_pipeline` returns a PipelineResult; both functions below were
# annotated `-> ScanResult`, which was simply wrong and made the module's
# contract unreadable from its signatures.
from backend.core.schemas import PipelineResult, ScanRequest, ScanResult  # noqa: F401
from backend.core import telemetry

# SELF-OBSERVATION: PostmortemAgent lives in a separate module so a missing or
# broken self_observer.py can never take down the breaker itself. Import is
# guarded — if it fails for any reason, postmortem generation silently no-ops
# everywhere below instead of raising at import time.
try:
    from backend.core.self_observer import PostmortemAgent
except Exception:  # noqa: BLE001 - intentionally broad; this is a soft dependency
    PostmortemAgent = None  # type: ignore[assignment,misc]

logger = logging.getLogger("devguard.resilience")

# --- Tunables (env-overridable) ---
CB_FAIL_MAX = int(os.getenv("CB_FAIL_MAX", "5"))
CB_RESET_TIMEOUT_S = float(os.getenv("CB_RESET_TIMEOUT_S", "30"))

PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "llama-3.3-70b-versatile")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "llama-3.1-8b-instant")

# Exceptions that should count as breaker failures. We treat AgentExecutionError
# plus generic timeouts as "the upstream is unhealthy." We deliberately do NOT
# trip the breaker on client errors (bad input) — that's not an upstream fault.
FAILURE_EXCEPTIONS = (AgentExecutionError, TimeoutError, ConnectionError)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when the breaker is OPEN and no fallback is available."""


# --------------------------------------------------------------------------- #
# SELF-OBSERVATION: lazy singleton accessor for PostmortemAgent.
#
# ASSUMPTION: self_observer.py exposes `PostmortemAgent` as a plain class with
# a no-arg constructor. If self_observer.py instead exposes its own
# lazy-singleton accessor (mirroring `get_mcp_client()` in mcp_client.py),
# swap the `PostmortemAgent()` construction below for that accessor call —
# nothing else in this file needs to change either way, since everything here
# only calls `agent.generate_postmortem(...)`.
# --------------------------------------------------------------------------- #
_postmortem_agent: Optional["PostmortemAgent"] = None


def _get_postmortem_agent() -> Optional["PostmortemAgent"]:
    """Return the process-wide PostmortemAgent, or None if unavailable.

    None is a valid, expected return here (missing module, construction
    failure, etc.) — callers must treat it as "skip postmortem generation,"
    never as something to raise on.
    """
    global _postmortem_agent
    if PostmortemAgent is None:
        return None
    if _postmortem_agent is None:
        try:
            _postmortem_agent = PostmortemAgent()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SELF-OBSERVATION: failed to construct PostmortemAgent (%s); "
                "postmortem generation disabled for this process.",
                exc,
            )
            return None
    return _postmortem_agent


class CircuitBreaker:
    """
    A minimal, thread-safe, async-friendly circuit breaker.

    We roll our own (rather than pybreaker) because we need first-class OTel
    integration on the state transitions — pybreaker's listener API works but a
    30-line custom breaker we fully control is cleaner for a reviewer to
    audit, and avoids surprising behavior around async call wrapping.
    """

    def __init__(
        self,
        name: str,
        fail_max: int = CB_FAIL_MAX,
        reset_timeout: float = CB_RESET_TIMEOUT_S,
    ) -> None:
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._state = CircuitState.CLOSED
        self._fail_count = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    def _transition(
        self,
        new_state: CircuitState,
        reason: str,
        error_context: Optional[str] = None,
    ) -> None:
        """Centralized state change so EVERY flip is observable.

        `error_context` is optional and only meaningful for transitions INTO
        OPEN — it carries a short description of the failure that tripped the
        breaker, used purely to seed the self-observation postmortem below.
        It does not affect the state machine itself.
        """
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        logger.warning(
            "CircuitBreaker[%s] %s -> %s (%s)", self.name, old, new_state, reason
        )
        # Emit as a span event on whatever span is active AND as a counter.
        telemetry.add_span_event(
            "circuit_breaker.state_change",
            {
                "breaker.name": self.name,
                "breaker.from": old.value,
                "breaker.to": new_state.value,
                "breaker.reason": reason,
            },
        )
        if telemetry.CIRCUIT_STATE_CHANGES is not None:
            telemetry.CIRCUIT_STATE_CHANGES.add(
                1,
                {
                    "breaker": self.name,
                    "from": old.value,
                    "to": new_state.value,
                },
            )

        # SELF-OBSERVATION: only an OPEN transition represents an incident
        # worth narrating — CLOSED (recovery) and HALF_OPEN (a probe attempt)
        # aren't failures in themselves. This call is fire-and-forget and
        # fully guarded; see `_schedule_postmortem` for why it can never block
        # or crash this synchronous method.
        if new_state == CircuitState.OPEN:
            self._schedule_postmortem(reason, error_context)

    def _schedule_postmortem(self, reason: str, error_context: Optional[str]) -> None:
        """SELF-OBSERVATION: schedule postmortem generation without blocking.

        This method is called from inside `_transition`, which itself runs
        under `self._lock` from synchronous call sites (`_on_success`,
        `_on_failure`, `_allow_call`). `loop.create_task(...)` only *schedules*
        a coroutine — it does not await it — so calling it here does not block
        the lock holder or make the breaker's state machine async. Nothing in
        this method may raise past its own boundary: a missing event loop or a
        scheduling failure must never affect the breaker's CLOSED/OPEN/HALF_OPEN
        logic above.
        """
        summary_parts = [f"breaker '{self.name}' opened: {reason}"]
        if error_context:
            summary_parts.append(error_context)
        recent_error_summary = " | ".join(summary_parts)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop (e.g. breaker tripped from sync test code,
            # or from a context outside the async app). Postmortems are a
            # nice-to-have observability layer, not a hard requirement, so we
            # just skip and move on.
            logger.debug(
                "CircuitBreaker[%s]: no running event loop; skipping postmortem "
                "generation for this OPEN transition.",
                self.name,
            )
            return

        try:
            loop.create_task(self._generate_and_record_postmortem(recent_error_summary))
        except Exception as exc:  # noqa: BLE001
            # Scheduling itself must never threaten the breaker's state machine.
            logger.warning(
                "CircuitBreaker[%s]: failed to schedule postmortem task (%s); "
                "breaker state transition is unaffected.",
                self.name,
                exc,
            )

    async def _generate_and_record_postmortem(self, recent_error_summary: str) -> None:
        """SELF-OBSERVATION: off-hot-path task that asks PostmortemAgent to
        narrate this incident, then records the result as a span event.

        Runs as a detached `asyncio` task (see `_schedule_postmortem`), so its
        latency — LLM calls are not fast — never delays the breaker flipping
        OPEN, the fallback engaging, or the user-facing scan response.
        `PostmortemAgent.generate_postmortem` is documented as fail-safe and
        non-raising, but we wrap in try/except anyway: a detached task that
        raises becomes an "unhandled exception in a task" warning at best and
        a swallowed crash at worst, and this feature must never be the reason
        anything else breaks.
        """
        agent = _get_postmortem_agent()
        if agent is None:
            logger.debug(
                "CircuitBreaker[%s]: PostmortemAgent unavailable; skipping "
                "postmortem for this OPEN transition.",
                self.name,
            )
            return

        try:
            postmortem_text = await agent.generate_postmortem(
                self.name, recent_error_summary
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CircuitBreaker[%s]: postmortem generation raised unexpectedly "
                "despite its fail-safe contract (%s); continuing without one.",
                self.name,
                exc,
            )
            return

        telemetry.add_span_event(
            "circuit_breaker.postmortem",
            {
                "breaker.name": self.name,
                "postmortem.text": postmortem_text,
            },
        )

        # SELF-OBSERVATION TODO: also append this postmortem to the durable
        # audit trail (backend/core/audit.py) so it survives past SigNoz's
        # retention window and shows up next to the scan's audit record. This
        # file doesn't have visibility into audit.py's write interface (entry
        # shape, hash-chaining call signature, etc.), so rather than guess at
        # a function that might not exist, this is left as an explicit
        # integration point for whoever owns that module. For now the
        # postmortem is durable only as a SigNoz span event.

    def _allow_call(self) -> bool:
        """Decide, at call time, whether the primary path may be attempted."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                # Has the cooldown elapsed? If so, allow ONE trial (half-open).
                if (
                    self._opened_at is not None
                    and (time.monotonic() - self._opened_at) >= self.reset_timeout
                ):
                    self._transition(CircuitState.HALF_OPEN, "cooldown elapsed")
                    return True
                return False
            # HALF_OPEN: allow the single probe. (We keep it simple: one at a time
            # is enforced by the fact that a real probe result flips us out fast.)
            return True

    def _on_success(self) -> None:
        with self._lock:
            self._fail_count = 0
            if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                self._transition(CircuitState.CLOSED, "probe succeeded")

    def _on_failure(self, exc: Optional[BaseException] = None) -> None:
        """
        `exc` (added): the exception that triggered this failure, if any.
        Threaded through from `call()` purely so an OPEN transition can seed
        the self-observation postmortem with real error context — it has no
        effect on the fail-count/threshold logic itself.
        """
        with self._lock:
            self._fail_count += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — slam back open and restart the cooldown clock.
                self._opened_at = time.monotonic()
                self._transition(
                    CircuitState.OPEN,
                    "half-open probe failed",
                    error_context=self._describe_failure(exc),
                )
            elif self._fail_count >= self.fail_max:
                self._opened_at = time.monotonic()
                self._transition(
                    CircuitState.OPEN,
                    f"fail_count {self._fail_count} >= fail_max {self.fail_max}",
                    error_context=self._describe_failure(exc),
                )

    @staticmethod
    def _describe_failure(exc: Optional[BaseException]) -> str:
        """SELF-OBSERVATION helper: short, human-readable failure description
        used only to seed the postmortem prompt. Never affects control flow."""
        if exc is None:
            return "no exception context captured"
        return f"{type(exc).__name__}: {exc}"

    async def call(self, func: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        """Execute an async callable under breaker protection."""
        if not self._allow_call():
            raise CircuitOpenError(
                f"Circuit '{self.name}' is OPEN; primary path suppressed."
            )
        try:
            result = await func(*args, **kwargs)
        except FAILURE_EXCEPTIONS as exc:
            self._on_failure(exc)
            raise
        else:
            self._on_success()
            return result


# Module-level breaker for the primary LLM provider (Groq).
_primary_breaker = CircuitBreaker(name="groq_primary")


def circuit_status() -> dict[str, Any]:
    """Expose breaker internals (used by /slo-status and dashboards)."""
    return {
        "name": _primary_breaker.name,
        "state": _primary_breaker.state.value,
        "fail_count": _primary_breaker._fail_count,
        "fail_max": _primary_breaker.fail_max,
        "reset_timeout_s": _primary_breaker.reset_timeout,
    }


@telemetry.traced("resilient_pipeline")
async def run_pipeline_resilient(request: ScanRequest) -> PipelineResult:
    """
    The single entry point the API layer calls instead of run_pipeline directly.

    Flow:
      1. Try primary model through the breaker.
      2. On CircuitOpenError (breaker suppressed primary) OR a failure that trips
         it, fall back to the smaller model for the cooldown window.
      3. If BOTH fail, record the exception on the span and re-raise — the API
         layer converts that into a traced 5xx (never a silent 500).

    SRE NOTE: We annotate the ScanResult / span with which model actually served
    the request ("served_by") and whether we degraded, so a judge (or on-call)
    can immediately see "this answer came from the fallback" during an incident.
    """
    span = telemetry.trace.get_current_span()

    # Attempt 1: primary via breaker.
    #
    # No model override — the pipeline's own routing stands. Forcing PRIMARY_MODEL
    # here would flatten the per-agent decisions that matter: the Scanner
    # deliberately always uses the strong model, and the Fixer may be adapted by
    # the self-observation layer.
    try:
        result = await _primary_breaker.call(_invoke, request, override_model=None)
        span.set_attribute("llm.served_by", _served_by(result))
        span.set_attribute("llm.degraded", False)
        return result
    except CircuitOpenError:
        # Breaker already open — skip straight to fallback, don't even try primary.
        telemetry.add_span_event(
            "degradation.fallback_engaged", {"reason": "circuit_open"}
        )
    except FAILURE_EXCEPTIONS as exc:
        # Primary attempt failed (and may have just tripped the breaker).
        telemetry.add_span_event(
            "degradation.fallback_engaged",
            {"reason": "primary_failure", "error.type": type(exc).__name__},
        )

    # Attempt 2: the degraded path. NOT wrapped in the same breaker, so a primary
    # outage doesn't also poison our escape hatch — and now genuinely degraded:
    # FALLBACK_MODEL is forced through every agent, so this is a different
    # provisioning rather than a retry of whatever just failed.
    span.set_attribute("llm.fallback_model_requested", FALLBACK_MODEL)
    try:
        result = await _invoke(request, override_model=FALLBACK_MODEL)
        # Report what actually served it, read off the result — not the constant
        # we asked for. These can differ, and during an incident the difference is
        # the whole point of looking.
        span.set_attribute("llm.served_by", _served_by(result))
        span.set_attribute("llm.degraded", True)
        return result
    except Exception as exc:  # noqa: BLE001
        # Total failure. Recorded on span by @traced; re-raise for the API layer.
        span.set_attribute("llm.served_by", "none")
        span.set_attribute("llm.degraded", True)
        raise


def _served_by(result: Any) -> str:
    """The model that actually ran, taken off the result.

    `llm.served_by` used to be stamped from the PRIMARY_MODEL / FALLBACK_MODEL
    constants, i.e. from what this module *intended*. That was not merely
    imprecise: `_invoke` discarded the model it was handed, so on the degraded
    path the attribute named a model that had not run. An on-call engineer
    reading a trace during an outage would have been told
    `served_by: llama-3.1-8b-instant` while the 70B model served the request.

    The Scanner's `model_used` is the right source: it is the first agent to run,
    every path goes through it, and it records the model it actually invoked.
    """
    model = getattr(getattr(result, "scan", None), "model_used", None)
    return model or "unknown"


@telemetry.traced("llm_invoke")
async def _invoke(
    request: ScanRequest, override_model: Optional[str] = None
) -> PipelineResult:
    """
    Thin wrapper around the AI module's run_pipeline.

    `override_model` is threaded all the way into the agents (see
    `ai_agent.run_pipeline`), so a degradation actually changes which model runs.
    It previously took a `model_id` that it recorded as a span attribute and then
    dropped, which is what made the "fallback" a retry in disguise.
    """
    telemetry.trace.get_current_span().set_attribute(
        "llm.model_id", override_model or "pipeline-routed"
    )
    return await run_pipeline(request.code, override_model=override_model)
