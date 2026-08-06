"""
backend/api/router.py
====================
FastAPI surface for DevGuard AI's observability + resilience layer.

SRE DESIGN NOTES — THE REQUEST LIFECYCLE
----------------------------------------
Every /scan request:
  1. Continues the frontend's trace (traceparent) -> ONE unbroken trace.
  2. Opens the PARENT span "scan_request".
  3. Checks Redis (cache_lookup child span).
  4. On miss, runs the resilient pipeline (breaker + fallback + agent child spans).
  5. Computes cost (cost_calc child span) + emits metrics.
  6. For critical/high severity -> PAUSE at approval gate (no auto-finalize).
  7. On finalize -> append to audit chain + record SLO sample.

ERROR HANDLING CONTRACT:
  - Bad input           -> 400/422 (never trips the breaker, never a 500).
  - Unknown scan_id      -> 404.
  - Pipeline/LLM failure -> 503 with trace_id in the body so support can jump
                            straight to the trace in SigNoz. NEVER an unhandled 500.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from opentelemetry import trace
from opentelemetry.trace import format_trace_id
from pydantic import BaseModel, ValidationError

from backend.core import audit, cache, languages, telemetry
from backend.core.ai_agent import AgentExecutionError
from backend.core.mcp_client import get_mcp_client
from backend.core.resilience import (
    CircuitOpenError,
    circuit_status,
    run_pipeline_resilient,
)
from backend.core.schemas import ScanRequest, ScanResult

logger = logging.getLogger("devguard.api")
router = APIRouter()

#: Where a real benchmark run leaves its numbers. Absent by default — the
#: harness has to be run for accuracy figures to exist. Overridable so a
#: deployment can point at a mounted artifact.
BENCHMARK_ARTIFACT_PATH = os.environ.get(
    "DEVGUARD_BENCHMARK_ARTIFACT", "data/benchmark_report.json"
)

# ---------------------------------------------------------------------------
# SLO tracking (Feature #6).
# ---------------------------------------------------------------------------
SLO_TARGET_PCT = 99.5
SLO_LATENCY_BUDGET_S = 3.0
SLO_WINDOW = 100
_slo_window: deque[bool] = deque(maxlen=SLO_WINDOW)  # True = met SLO (under 3s)


def _record_slo_sample(latency_s: float) -> None:
    _slo_window.append(latency_s <= SLO_LATENCY_BUDGET_S)


def _slo_snapshot() -> dict[str, Any]:
    total = len(_slo_window)
    if total == 0:
        return {
            "slo_target": SLO_TARGET_PCT,
            "latency_objective_s": SLO_LATENCY_BUDGET_S,
            "window_size": SLO_WINDOW,
            "samples": 0,
            "current_compliance_pct": 100.0,
            "error_budget_remaining": 100.0,
        }
    met = sum(1 for ok in _slo_window if ok)
    compliance = round(100.0 * met / total, 3)
    allowed_bad = (100.0 - SLO_TARGET_PCT) / 100.0 * total
    actual_bad = total - met
    remaining = 100.0 if allowed_bad == 0 else round(
        max(0.0, 100.0 * (allowed_bad - actual_bad) / allowed_bad), 3
    )
    return {
        "slo_target": SLO_TARGET_PCT,
        "latency_objective_s": SLO_LATENCY_BUDGET_S,
        "window_size": SLO_WINDOW,
        "samples": total,
        "current_compliance_pct": compliance,
        "error_budget_remaining": remaining,
    }


# ---------------------------------------------------------------------------
# Pending-approval state (Feature #7).
# ---------------------------------------------------------------------------
_pending_approvals: dict[str, dict[str, Any]] = {}

# WebSocket subscribers per scan_id, for live waterfall streaming (Feature #10).
_ws_subscribers: dict[str, set[WebSocket]] = {}
_ws_pending_events: dict[str, list[dict]] = {}  # buffer for events published before a subscriber connects
_scan_results: dict[str, dict[str, Any]] = {}  # scan_id -> full frontend-shaped result

# ---------------------------------------------------------------------------
# RETENTION FOR THE THREE IN-MEMORY DICTS ABOVE.
#
# All three are keyed by scan_id and all three used to grow without bound:
#
#   _scan_results       had NO eviction path anywhere in this file. Every scan
#                       added an entry and nothing removed it. Each entry holds
#                       `original_code` AND `fixed_code`, and ScanRequest.code is
#                       capped at 50,000 characters, so one entry measures ~53
#                       KiB — 52 MiB at a thousand scans, 521 MiB at ten
#                       thousand, retained until the process dies. The durable
#                       record that actually needs to survive is the audit log.
#   _pending_approvals  is popped on /approve and /reject, but a gated scan that
#                       nobody decides stays forever holding the submitted
#                       source. Critical/high is what gets gated, so the entries
#                       that linger hold the most sensitive code. The dict
#                       already carried a `created_at` that nothing ever read.
#   _ws_pending_events  is drained only when a WebSocket subscriber connects. A
#                       curl-driven scan, or a user who closes the tab first,
#                       leaves its buffer resident forever.
#
# Bounded by BOTH count and age on purpose: a count cap alone lets a quiet
# deployment hold submitted code for the life of the process, and a TTL alone
# lets a burst spike memory before anything ages out.
# ---------------------------------------------------------------------------

#: How many finished scan results to keep for `GET /scan/{scan_id}`. 500 x ~53
#: KiB worst case is ~26 MiB, which a small container survives. Overridable for
#: deployments that page back through more history.
SCAN_RESULT_MAX_ENTRIES = int(os.environ.get("DEVGUARD_SCAN_RESULT_MAX", "500"))

#: How long a finished result stays fetchable. The frontend loads
#: `GET /scan/{id}` immediately after `POST /scan`, so this only has to outlast a
#: page load by a wide margin — an hour lets a user leave a tab open and come back.
SCAN_RESULT_TTL_SECONDS = float(os.environ.get("DEVGUARD_SCAN_RESULT_TTL_S", "3600"))

#: How long an undecided approval gate is held. Long enough for a human to
#: actually review a critical finding; not so long that abandoned gates
#: accumulate submitted source. Expiry here discards a *pending* decision, never
#: a recorded one — the audit log is written at decide time, not at gate time.
PENDING_APPROVAL_TTL_SECONDS = float(
    os.environ.get("DEVGUARD_PENDING_APPROVAL_TTL_S", "86400")
)

#: Cap on buffered WebSocket events per scan. The newest are what a late
#: subscriber needs — the terminal "done" event is always last.
WS_BUFFER_MAX_EVENTS = int(os.environ.get("DEVGUARD_WS_BUFFER_MAX", "200"))

#: scan_id -> monotonic timestamp when the result was stored. Separate from
#: `_scan_results` so the response payload stays exactly the API contract.
_scan_result_stored_at: dict[str, float] = {}


def _remember_scan_result(
    scan_id: str, result: dict[str, Any], now: Optional[float] = None
) -> None:
    """Store a finished result for `GET /scan/{scan_id}`, then sweep."""
    _scan_results[scan_id] = result
    _scan_result_stored_at[scan_id] = now if now is not None else time.monotonic()
    _evict_scan_state()


def _buffer_ws_event(scan_id: str, payload: dict[str, Any]) -> None:
    """Buffer one event for a subscriber that has not connected yet, bounded."""
    buf = _ws_pending_events.setdefault(scan_id, [])
    buf.append(payload)
    if len(buf) > WS_BUFFER_MAX_EVENTS:
        # Drop from the front: a late subscriber cares about the recent events,
        # and the terminal "done" event it waits on is always the last one.
        del buf[: len(buf) - WS_BUFFER_MAX_EVENTS]


def _evict_scan_state(
    max_entries: Optional[int] = None, now: Optional[float] = None
) -> None:
    """Drop scan state that is too old or too plentiful to keep.

    Cheap enough to call on every store: it is O(expired) in the common case and
    only sorts when the count cap is actually exceeded.
    """
    cap = SCAN_RESULT_MAX_ENTRIES if max_entries is None else max_entries
    mono = time.monotonic() if now is None else now

    # 1. Age out results.
    expired = [
        sid for sid, stored in _scan_result_stored_at.items()
        if mono - stored > SCAN_RESULT_TTL_SECONDS
    ]
    for sid in expired:
        _forget_scan(sid)

    # 2. Then trim to the count cap, oldest first.
    if len(_scan_results) > cap:
        by_age = sorted(
            _scan_results,
            key=lambda sid: _scan_result_stored_at.get(sid, 0.0),
        )
        for sid in by_age[: len(_scan_results) - cap]:
            _forget_scan(sid)

    # 3. Age out undecided approval gates. `created_at` is wall-clock time.
    #    A missing timestamp is never a reason to discard a pending human
    #    decision, so a malformed entry is left alone.
    wall = time.time()
    for sid, entry in list(_pending_approvals.items()):
        created = entry.get("created_at")
        if created is None:
            continue
        if wall - created > PENDING_APPROVAL_TTL_SECONDS:
            logger.info(
                "Approval gate for scan_id=%s expired after %.0fs undecided; "
                "discarding the pending state. No audit entry was written for it.",
                sid, wall - created,
            )
            _pending_approvals.pop(sid, None)
            _forget_scan(sid)

    # 4. Drop event buffers whose scan is no longer known to us at all — nobody
    #    can subscribe to a scan we have already forgotten.
    for sid in list(_ws_pending_events):
        if (
            sid not in _scan_results
            and sid not in _pending_approvals
            and sid not in _ws_subscribers
        ):
            _ws_pending_events.pop(sid, None)


def _forget_scan(scan_id: str) -> None:
    """Release every in-memory trace of one scan. The audit log is untouched."""
    _scan_results.pop(scan_id, None)
    _scan_result_stored_at.pop(scan_id, None)
    if scan_id not in _ws_subscribers:
        _ws_pending_events.pop(scan_id, None)


async def _publish_span_event(scan_id: str, payload: dict[str, Any]) -> None:
    """Push a span-completion event to all WS subscribers for this scan.
    If no subscriber is connected yet (the POST /scan pipeline usually
    finishes before the frontend opens its WebSocket), buffer the event so
    it can be replayed the instant a subscriber connects."""
    subs = _ws_subscribers.get(scan_id, set())
    if not subs:
        _buffer_ws_event(scan_id, payload)
        return
    dead = set()
    for ws in subs:
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001
            dead.add(ws)
    for ws in dead:
        subs.discard(ws)


def _load_benchmark_artifact() -> Optional[dict[str, Any]]:
    """Read the accuracy figures the harness last measured, or None.

    the measured-numbers rule: every published number comes from the machine. This response used to
    carry `{"accuracy": 0.9, "precision": 0.92, "recall": 0.88,
    "false_positive_rate": 0.05, "sample_size": 14}` as hand-typed literals,
    and the result page rendered them as an "Accuracy benchmark proof strip" on
    every scan — while the README correctly stated the harness "has never been
    run to an artifact, so no accuracy figures are published anywhere."

    Now the only source is an artifact written by an actual run
    (`python -m backend.core.benchmark --json > data/benchmark_report.json`).
    No artifact means no figures: the field is `null` and the UI says so. A
    corrupt or partial artifact is also `null` — never a half-populated strip,
    because a missing `recall` rendering as 0% reads as a measured 0% recall.
    """
    required = ("accuracy", "precision", "recall", "false_positive_rate", "sample_size")
    try:
        with open(BENCHMARK_ARTIFACT_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Benchmark artifact at %s is unreadable (%s); publishing no accuracy "
            "figures rather than substituting any.", BENCHMARK_ARTIFACT_PATH, exc,
        )
        return None

    if not isinstance(data, dict) or any(data.get(k) is None for k in required):
        logger.warning(
            "Benchmark artifact at %s is missing required metrics %s; publishing "
            "no accuracy figures.", BENCHMARK_ARTIFACT_PATH,
            [k for k in required if not isinstance(data, dict) or data.get(k) is None],
        )
        return None

    out = {k: data[k] for k in required}
    # Provenance, so the UI can date the figures instead of implying they are
    # from this scan. Optional: an older artifact simply has no timestamp.
    if data.get("generated_at") is not None:
        out["generated_at"] = data["generated_at"]
    return out


#: Sentinel `finalized_entry` value meaning "the append was attempted and it
#: raised", as distinct from None ("not attempted yet").
AUDIT_APPEND_FAILED = "append-failed"


def _audit_entry_block(
    scan_id: str, code_hash: str, finalized_entry: Optional[Any],
) -> dict[str, Any]:
    """The audit-chain facts for one scan, reporting only what is true yet."""
    if finalized_entry is AUDIT_APPEND_FAILED:
        return {
            "scan_id": scan_id,
            "code_hash": code_hash,
            "prev_hash": None,
            "entry_hash": None,
            "chain_verified": False,
            "chain_state": "failed",
        }
    if isinstance(finalized_entry, dict):
        return {
            "scan_id": scan_id,
            "code_hash": code_hash,
            "prev_hash": finalized_entry.get("prev_hash"),
            "entry_hash": finalized_entry.get("entry_hash"),
            # The append itself computed and wrote these links, so the record IS
            # chained. This is not a claim about the whole file — that is what
            # GET /audit-log/verify is for, and it walks every entry.
            "chain_verified": True,
            "chain_state": "written",
        }
    return {
        "scan_id": scan_id,
        "code_hash": code_hash,
        "prev_hash": None,
        "entry_hash": None,
        "chain_verified": None,
        "chain_state": "pending",
    }


def _sum_tokens(*results) -> Optional[int]:
    """Total provider-reported tokens across the agents that reported any.

    Returns None when no agent reported usage at all — that is "nothing counted
    them", which is a different fact from "zero tokens were used" and must not
    render as a measured 0 on the "Tokens Used" card.
    """
    counted = [
        t for t in (getattr(r, "tokens_used", None) for r in results if r is not None)
        if t is not None
    ]
    return sum(counted) if counted else None


def _build_frontend_result(
    scan_id: str, req: ScanRequest, pipeline, trace_id: str,
    latency_ms: float, cost: Optional[float], status: str = "complete",
    finalized_entry: Optional[dict[str, Any]] = None,
    cached_hit: bool = False,
) -> dict[str, Any]:
    """Reshape the internal PipelineResult into the exact JSON contract the
    Result Dashboard (frontend/app/result/page.tsx) expects.

    Everything this function emits is rendered to a human as measured fact, so
    every field is either a real measurement or explicitly absent (`null`).
    `finalized_entry` is the audit record `_finalize` actually wrote, when it has
    been written by the time this runs; without it the chain state is reported as
    `pending` rather than asserted as verified.
    """
    scan = getattr(pipeline, "scan", None)
    final_fix = getattr(pipeline, "final_fix", None)
    final_validation = getattr(pipeline, "final_validation", None)
    reflection_history = getattr(pipeline, "reflection_history", []) or []
    routing = getattr(pipeline, "routing_decisions", {}) or {}

    severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    vulnerabilities = []
    max_sev = "low"
    if scan is not None:
        for v in getattr(scan, "vulnerabilities", []) or []:
            sev = str(getattr(v, "severity", "low")).replace("Severity.", "").lower()
            vulnerabilities.append({
                "cwe": getattr(v, "cwe_id", "UNKNOWN"),
                "title": (getattr(v, "explanation", "") or "")[:80],
                "line": getattr(v, "line_number", 0),
                "severity": sev,
            })
            if severity_order.get(sev, 0) > severity_order.get(max_sev, 0):
                max_sev = sev

    retry_history = []
    for attempt in reflection_history:
        val = getattr(attempt, "validation", None)
        verdict_str = str(getattr(val, "verdict", "")) if val else ""
        retry_history.append({
            "attempt": getattr(attempt, "attempt_number", 0),
            "agent": "fixer",
            "validator_score": getattr(val, "eval_score", 0) if val else 0,
            "passed": "pass" in verdict_str.lower(),
        })

    eval_score = getattr(final_validation, "eval_score", 0) if final_validation else 0

    # SEVERITY, NOT CVSS. This used to publish `cvss_before` from a hard-coded
    # {"critical": 9.5, "high": 7.8, ...} lookup and `cvss_after` as the constant
    # 1.2, both rendered with `.toFixed(1)` under a "CVSS" label — which claims a
    # scored CVSS vector for code nobody scored. The Scanner reports a severity
    # *band*, so that ordinal is what gets published, under its own name.
    #
    # There is deliberately no "after" value: nothing re-scores the patched code.
    # The Validator grades the patch (eval_score, already reported above), which
    # is a different measurement from the residual severity of the result.
    severity_before = max_sev if vulnerabilities else None

    slo_snap = _slo_snapshot()
    budget = slo_snap["error_budget_remaining"]
    slo_state = "green" if budget > 50 else ("amber" if budget > 0 else "red")

    code_hash = telemetry._sha256(getattr(req, "code", ""))

    return {
        "original_code": getattr(pipeline, "original_code", getattr(req, "code", "")),
        "fixed_code": getattr(final_fix, "patched_code", "") if final_fix else "",
        "language": getattr(req, "language", "python"),
        "vulnerabilities": vulnerabilities,
        "eval_score": eval_score,
        "severity_before": severity_before,
        "retry_history": retry_history,
        "model_routing": {
            "tier": routing.get("scanner", "unknown"),
            "reason": f"severity-based routing ({max_sev})",
        },
        "latency_ms": round(latency_ms, 1),
        # SPEND ON *THIS* REQUEST vs spend that produced the result.
        #
        # A cache hit makes zero LLM calls, so it consumed zero tokens and cost
        # nothing — but the cached PipelineResult still carries the ORIGINAL
        # run's figures. Billing them again on every repeat scan would
        # double-count spend, and it only became reachable once the cache was
        # fixed to actually hit (it never had before).
        #
        # So: `tokens_used` / `cost_usd` describe THIS request. The original
        # run's figures are still reported, under names that say whose they are,
        # because they are the honest answer to "what did producing this result
        # take?".
        "cached": cached_hit,
        "tokens_used": None if cached_hit else _sum_tokens(scan, final_fix, final_validation),
        "cost_usd": 0.0 if cached_hit else cost,
        "origin_tokens_used": _sum_tokens(scan, final_fix, final_validation),
        "origin_cost_usd": _sum_cost(scan, final_fix, final_validation),
        "slo_status": {
            "slo_target": SLO_TARGET_PCT,
            "error_budget_remaining_pct": budget,
            "state": slo_state,
        },
        # THE CHAIN BADGE. This block used to be
        # `{"prev_hash": "", "chain_verified": True}` — a hard-coded True that
        # rendered a green "✅ Chain Verified" tamper-evidence badge on the result
        # page. It was also computed BEFORE the entry existed: `_finalize` appends
        # the record after this function returns, so the response asserted a
        # verified hash chain for a record that had not been written.
        #
        # Three states now, and they are distinguishable:
        #   pending  -> not written yet, `chain_verified: null` (the UI says
        #               "pending", not "broken" — reporting False here would cry
        #               tamper on every healthy scan)
        #   written  -> the real prev_hash/entry_hash the append produced
        #   failed   -> the append raised; the entry is NOT in the log
        "audit_entry": _audit_entry_block(scan_id, code_hash, finalized_entry),
        # Accuracy figures come from a real harness run or not at all.
        "benchmark_report": _load_benchmark_artifact(),
        "trace_id": trace_id,
        "spans": [],
        "status": status,
    }


# ===========================================================================
# POST /scan  — the main entry point
# ===========================================================================
@router.post("/scan")
async def scan(request: Request, x_traceparent: Optional[str] = Header(default=None)):
    # ---- Input parsing (400/422, never a 500) ----
    try:
        body = await request.json()
        scan_req = ScanRequest(**body)
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid scan request: {exc}")

    # ---- Continue the frontend's trace (Feature #2) ----
    ctx = telemetry.extract_context(dict(request.headers))
    tracer = telemetry.get_tracer()
    scan_id = str(uuid.uuid4())

    with tracer.start_as_current_span("scan_request", context=ctx) as parent:
        trace_id = format_trace_id(parent.get_span_context().trace_id)
        parent.set_attribute("scan.id", scan_id)
        # Severity is a FINDING, not a request field — ScanRequest has only
        # `code` and `language`. This attribute used to read a field that does
        # not exist and stamped "unknown" on every trace. It is set after the
        # scan instead, where the value is actually known.

        started = time.perf_counter()
        try:
            # ---- Cache lookup ----
            with telemetry.start_span("cache_lookup"):
                cached = await cache.get_cached(scan_req)
            if cached is not None:
                await _publish_span_event(
                    scan_id, {"stage": "cache", "status": "hit", "duration_ms": 0}
                )
                latency = time.perf_counter() - started
                _record_slo_sample(latency)
                _emit_request_metrics(cached, latency, cache_hit=True)
                return await _finalize_or_gate(scan_id, scan_req, cached, trace_id, latency, cached_hit=True)

            # ---- Run the resilient pipeline (breaker + fallback) ----
            result = await run_pipeline_resilient(scan_req)

            # ---- Cost calc (child span) ----
            with telemetry.start_span("cost_calc") as cost_span:
                cost = _compute_and_record_cost(result, cost_span)

            await cache.set_cached(scan_req, result)
            await _publish_span_event(
                scan_id,
                {
                    "stage": "pipeline",
                    "status": "ok",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "cost_usd": cost,
                },
            )

            latency = time.perf_counter() - started
            _record_slo_sample(latency)
            _emit_request_metrics(result, latency, cache_hit=False)

            return await _finalize_or_gate(scan_id, scan_req, result, trace_id, latency, cached_hit=False, cost=cost)

        except CircuitOpenError as exc:
            latency = time.perf_counter() - started
            _record_slo_sample(latency)
            parent.set_attribute("error.type", "CircuitOpenError")
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "LLM provider temporarily unavailable (circuit open).",
                    "trace_id": trace_id,
                    "retry_after_s": circuit_status()["reset_timeout_s"],
                },
            )
        except AgentExecutionError as exc:
            latency = time.perf_counter() - started
            _record_slo_sample(latency)
            raise HTTPException(
                status_code=503,
                detail={"error": f"Scan pipeline failed: {exc}", "trace_id": trace_id},
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — the catch-all that prevents raw 500s
            latency = time.perf_counter() - started
            _record_slo_sample(latency)
            parent.record_exception(exc)
            parent.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            logger.exception("Unhandled scan error (trace_id=%s)", trace_id)
            raise HTTPException(
                status_code=500,
                detail={"error": "Internal error", "trace_id": trace_id},
            )


async def _finalize_or_gate(
    scan_id: str, req: ScanRequest, result, trace_id: str,
    latency_s: float = 0.0, cached_hit: bool = False, cost: Optional[float] = None,
) -> dict[str, Any]:
    """
    Human-in-the-loop gate (Feature #7): critical/high severity fixes PAUSE for
    approval instead of auto-finalizing. Everything else finalizes immediately.

    The audit append for the low/medium path is AWAITED here, not fired as a
    background task. It used to be `asyncio.create_task(_finalize(...))`, which
    had three problems: the docstring claimed the write was synchronous when it
    was not; an append that raised was swallowed with no log and no signal, so
    the audit record was silently lost while the response had already told the
    client the chain was verified; and asyncio keeps only a weak reference to a
    bare task, so it could be garbage-collected before it ran. The append is a
    sub-millisecond off-thread file write (see audit.append_entry), so awaiting
    it costs the request nothing measurable and makes the reported chain state
    true.
    """
    severity = _gating_severity(result)
    verdict = _verdict_of(result)
    score = _extract_score(result)
    gated = severity in GATED_SEVERITIES

    # Critical/high pause at the gate, so no entry is written yet -> the chain
    # state is reported as "pending" until /approve or /reject writes one.
    finalized_entry: Optional[Any] = None
    if not gated:
        try:
            finalized_entry = await _finalize(scan_id, req, result)
        except Exception:  # noqa: BLE001 — the audit write must not 500 the scan
            logger.exception(
                "Audit append FAILED for scan_id=%s; the scan result stands but "
                "this scan is NOT in the audit chain.", scan_id,
            )
            telemetry.add_span_event("audit.append_failed", {"scan.id": scan_id})
            finalized_entry = AUDIT_APPEND_FAILED

    # Severity is known now that the scan has run — stamp it on the trace here,
    # where it is a measurement rather than an absent request field.
    telemetry.add_span_event(
        "scan.severity_determined",
        {"scan.id": scan_id, "scan.severity": severity or "none", "scan.gated": gated},
    )

    frontend_result = _build_frontend_result(
        scan_id, req, result, trace_id, latency_s * 1000, cost,
        status="pending_approval" if gated else "complete",
        finalized_entry=finalized_entry,
        cached_hit=cached_hit,
    )
    _remember_scan_result(scan_id, frontend_result)

    if gated:
        _pending_approvals[scan_id] = {
            "request": req,
            "result": result,
            "trace_id": trace_id,
            "created_at": time.time(),
        }
        telemetry.add_span_event(
            "approval.gate_opened", {"scan.id": scan_id, "severity": severity}
        )
        asyncio.create_task(
            _publish_span_event(
                scan_id,
                {
                    "type": "span",
                    "agent": "validator",
                    "status": "completed",
                    "message": f"Pending human approval (severity={severity}).",
                },
            )
        )
        return {
            "scan_id": scan_id,
            "status": "pending_approval",
            "severity": severity,
            "verdict": verdict,
            "trace_id": trace_id,
            "message": "High-severity fix requires human approval before finalization.",
        }

    # Low/medium: the audit entry was already written above, awaited.
    # Tell any (current or future, thanks to the buffer) WebSocket subscriber
    # that this scan is done — this is the event the frontend actually waits on.
    asyncio.create_task(
        _publish_span_event(
            scan_id, {"type": "done", "scan_id": scan_id, "score": score}
        )
    )
    return {
        "scan_id": scan_id,
        "status": "cache_hit" if cached_hit else "completed",
        "severity": severity,
        "verdict": verdict,
        "trace_id": trace_id,
        "result": _dump(result),
    }


#: Finding severities that require a human decision before the fix is finalized.
GATED_SEVERITIES = ("critical", "high")

#: Ordering for "worst finding in the batch".
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _gating_severity(result) -> Optional[str]:
    """The worst severity the Scanner reported, or None if it found nothing.

    THIS IS WHAT DECIDES WHETHER A HUMAN REVIEWS THE FIX, and it used to read a
    field that does not exist:

        severity = str(getattr(req, "severity", "")).lower()
        gated = severity in ("critical", "high")

    `req` is a `ScanRequest`, whose fields are exactly `['code', 'language']` —
    there is no `severity`. Pydantic drops the extra key if a caller supplies
    one, so `getattr` returned `""` every time and `gated` was permanently
    `False`. **The human-in-the-loop approval gate never opened.** Every scan
    auto-finalized, critical ones included; `POST /scan/{id}/approve` and
    `/reject` were unreachable, and the `pending_approval` status was never
    emitted.

    The design was wrong twice over. Even with the field present, the *request*
    cannot know the severity — nothing has scanned the code when it arrives.
    Severity is a finding, not an input, and the pipeline already computes this
    exact value to route the Fixer's model.

    Reading it from the scan also closes a bypass: a client cannot send
    `severity: "low"` to talk the gate out of opening on a critical finding.
    """
    scan = getattr(result, "scan", None)
    vulns = getattr(scan, "vulnerabilities", None) or []
    worst: Optional[str] = None
    for v in vulns:
        sev = getattr(getattr(v, "severity", None), "value", None) or str(
            getattr(v, "severity", "")
        ).replace("Severity.", "").lower()
        if sev not in _SEVERITY_RANK:
            # An unrecognised severity must not silently rank as harmless. Gate
            # on it: a human looking at one extra fix is a far cheaper mistake
            # than an unreviewed critical patch shipping itself.
            logger.warning(
                "Unrecognised finding severity %r; treating it as gated so it "
                "cannot skip human review.", sev,
            )
            return "critical"
        if worst is None or _SEVERITY_RANK[sev] > _SEVERITY_RANK[worst]:
            worst = sev
    return worst


def _verdict_of(result) -> str:
    """The pipeline's decision, as the audit trail should record it.

    THIS IS THE AUDIT TRAIL'S ONE SUBSTANTIVE FIELD, and it was never written.
    Both this and the `/scan` response used:

        str(getattr(result, "verdict", "unknown"))

    `result` is a `PipelineResult`, which has no `verdict` field — the verdict
    lives at `final_validation.verdict`. So the `getattr` default won every
    time. Measured against the log committed in this repository: **35 of 35
    entries recorded the literal string "unknown"**. A cryptographically sound,
    tamper-evident chain of records that say nothing.

    Four distinguishable outcomes, because collapsing them is how the defect
    stayed invisible:

      pass / fail   the Validator's actual judgement
      no_findings   the Scanner reported nothing, so no Fixer and no Validator
                    ran. Recording "pass" here would claim a Validator judgement
                    that never happened (the no-fabrication rule).
      unvalidated   there were findings but no final validation — retries
                    exhausted, or the loop ended without a verdict. A real state,
                    and distinct from "the read failed", which is what "unknown"
                    now means and nothing else.

    `Verdict` subclasses `str`, so `str(Verdict.PASS)` yields "Verdict.PASS".
    `.value` is used deliberately: a record reading "Verdict.PASS" is
    machine-hostile and betrays that the value was stringified rather than read.
    """
    validation = getattr(result, "final_validation", None)
    if validation is not None:
        verdict = getattr(validation, "verdict", None)
        if verdict is not None:
            return getattr(verdict, "value", None) or str(verdict)

    scan = getattr(result, "scan", None)
    if scan is not None and not getattr(scan, "vulnerabilities", None):
        return "no_findings"
    if scan is not None:
        return "unvalidated"
    return "unknown"


async def _finalize(scan_id: str, req: ScanRequest, result) -> dict[str, Any]:
    """Write the immutable audit entry (Feature #8) and return the record written.

    Returning it lets the caller report the real prev_hash/entry_hash instead of
    asserting a chain link it never saw. Raises on failure — callers decide what
    to do, but nobody may report a verified chain for a write that did not happen.
    """
    code_hash = telemetry._sha256(getattr(req, "code", ""))
    return await audit.append_entry(
        scan_id=scan_id, code_hash=code_hash, verdict=_verdict_of(result)
    )


# ===========================================================================
# GET /scan/{scan_id}  — fetch the stored result for the Result Dashboard
# ===========================================================================
@router.get("/scan/{scan_id}")
async def get_scan(scan_id: str):
    result = _scan_results.get(scan_id)
    if result is None:
        if scan_id in _pending_approvals:
            raise HTTPException(status_code=202, detail="Scan still processing.")
        raise HTTPException(status_code=404, detail="Scan not found.")
    return result


# ===========================================================================
# Approval / rejection (Feature #7)
# ===========================================================================
@router.post("/scan/{scan_id}/approve")
async def approve(scan_id: str):
    entry = _pending_approvals.get(scan_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No pending approval for scan_id.")
    with telemetry.start_span("approval_decision") as span:
        span.set_attribute("scan.id", scan_id)
        span.set_attribute("approval.decision", "approved")
        telemetry.add_span_event("approval.approved", {"scan.id": scan_id})
        written = await _finalize(scan_id, entry["request"], entry["result"])
    score = _extract_score(entry["result"])
    if scan_id in _scan_results:
        stored = _scan_results[scan_id]
        stored["status"] = "complete"
        # The gated result was stored with chain_state "pending" because no entry
        # existed yet. It does now, so replace the placeholder with the real
        # links rather than leaving the dashboard showing a stale pending badge.
        stored["audit_entry"] = _audit_entry_block(
            scan_id, stored["audit_entry"]["code_hash"], written
        )
    await _publish_span_event(
        scan_id, {"type": "done", "scan_id": scan_id, "score": score}
    )
    _pending_approvals.pop(scan_id, None)
    return {
        "scan_id": scan_id,
        "status": "approved",
        "verdict": _verdict_of(entry["result"]),
    }


@router.post("/scan/{scan_id}/reject")
async def reject(scan_id: str, reason: Optional[str] = None):
    entry = _pending_approvals.get(scan_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No pending approval for scan_id.")
    with telemetry.start_span("approval_decision") as span:
        span.set_attribute("scan.id", scan_id)
        span.set_attribute("approval.decision", "rejected")
        span.set_attribute("approval.reason", reason or "unspecified")
        telemetry.add_span_event("approval.rejected", {"scan.id": scan_id, "reason": reason or ""})
        code_hash = telemetry._sha256(getattr(entry["request"], "code", ""))
        written = await audit.append_entry(
            scan_id=scan_id, code_hash=code_hash, verdict="rejected"
        )
    if scan_id in _scan_results:
        stored = _scan_results[scan_id]
        stored["status"] = "error"
        # A rejection is also an audited decision — the entry exists, so report it.
        stored["audit_entry"] = _audit_entry_block(scan_id, code_hash, written)
    await _publish_span_event(
        scan_id, {"type": "error", "message": f"Fix rejected: {reason or 'no reason given'}"}
    )
    _pending_approvals.pop(scan_id, None)
    return {"scan_id": scan_id, "status": "rejected", "reason": reason}


# ===========================================================================
# Supported languages
# ===========================================================================
@router.get("/languages")
async def supported_languages():
    """
    The languages the Scanner pipeline actually supports.

    Served from `backend.core.languages.LANGUAGES` — the same registry the
    agent prompts and the upload extension mapping read — so the picker in the
    UI cannot advertise a language the pipeline does not handle. Adding a
    language is a one-line change to that registry, and this endpoint plus the
    frontend follow automatically.
    """
    return {
        "default": languages.DEFAULT_LANGUAGE,
        "languages": [
            {
                "id": lang.id,
                "label": lang.label,
                "extensions": list(lang.extensions),
            }
            for lang in languages.LANGUAGES
        ],
    }


# ===========================================================================
# SLO status (Feature #6)
# ===========================================================================
@router.get("/slo-status")
async def slo_status():
    snap = _slo_snapshot()
    snap["circuit_breaker"] = circuit_status()
    return snap


@router.get("/telemetry-status")
async def telemetry_status():
    """Honest, machine-readable statement of what observability is actually on.

    Exists so the SigNoz/MCP claim is inspectable at runtime rather than taken
    on trust from a README. Every field here is read from live configuration or
    a real object — none of it is asserted.
    """
    client = get_mcp_client()
    return {
        "otlp": {
            "endpoint": telemetry.OTLP_ENDPOINT,
            "sdk_disabled": os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true",
            "service_name": telemetry.SERVICE_NAME,
            # Whether spans are being *emitted*. Whether anything is listening
            # at the other end is a separate question this cannot answer.
            "exporter_configured": bool(telemetry.OTLP_ENDPOINT),
        },
        "signoz_mcp": client.capability_report(),
        "self_observation_source": (
            "signoz_mcp" if client.is_configured() else "local_shadow"
        ),
    }


# ===========================================================================
# Audit log + verification (Feature #8)
# ===========================================================================
@router.get("/audit-log")
async def get_audit_log(limit: int = 100):
    if limit < 1 or limit > 10_000:
        raise HTTPException(status_code=400, detail="limit must be 1..10000")
    # read_tail parses only the page it returns, and both calls go to a thread
    # so a large audit log cannot stall the event loop. This endpoint used to
    # json.loads() the entire log to produce one page — 311 ms at 50k entries.
    count, entries = await asyncio.gather(
        asyncio.to_thread(audit.count_entries),
        asyncio.to_thread(audit.read_tail, limit),
    )
    return {"count": count, "entries": entries}


@router.get("/audit-log/verify")
async def verify_audit_log():
    """Re-walk the whole hash chain and report the first break, if any.

    `verify_chain()` is inherently O(N) — verifying a chain means hashing every
    record, and that cost is not reducible. What IS avoidable is paying it on the
    event loop, which is what this handler used to do: FastAPI runs `async def`
    handlers on the loop, so a synchronous walk stalled every concurrent scan,
    SLO poll and WebSocket frame for its full duration. Measured on real files,
    the block was 70 ms at 10k entries and 353 ms at 50k, and the audit log only
    grows — so a single-worker deployment degraded monotonically over its life.

    `asyncio.to_thread` keeps the walk off the loop. Same offload
    `GET /audit-log` already used; this endpoint was simply missed.
    `tests/test_endpoint_event_loop.py` measures loop starvation directly rather
    than trusting inspection.
    """
    report = await asyncio.to_thread(audit.verify_chain)
    if not report["valid"]:
        raise HTTPException(status_code=409, detail=report)
    return report


# ===========================================================================
# WebSocket live waterfall (Feature #10)
# ===========================================================================
@router.websocket("/ws/scan/{scan_id}")
async def ws_scan(websocket: WebSocket, scan_id: str):
    await websocket.accept()
    _ws_subscribers.setdefault(scan_id, set()).add(websocket)
    try:
        await websocket.send_json({"scan_id": scan_id, "status": "subscribed"})
        pending = _ws_pending_events.pop(scan_id, [])
        for payload in pending:
            await websocket.send_json(payload)
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "keepalive", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        subs = _ws_subscribers.get(scan_id, set())
        subs.discard(websocket)
        if not subs:
            _ws_subscribers.pop(scan_id, None)


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
def _agents_of(result) -> tuple:
    """The per-agent results a pipeline run produced, in execution order."""
    return (
        getattr(result, "scan", None),
        getattr(result, "final_fix", None),
        getattr(result, "final_validation", None),
    )


def _sum_cost(*results) -> Optional[float]:
    """Total USD across the agents that reported a cost, or None if none did.

    None, not 0.0 — "nothing counted it" is a different fact from "this scan was
    free", and only one of them is ever true of a scan that made paid calls.
    """
    counted = [
        c for c in (getattr(r, "cost_usd", None) for r in results if r is not None)
        if c is not None
    ]
    return round(sum(counted), 8) if counted else None


def _compute_and_record_cost(result, span) -> Optional[float]:
    """Per-request cost, summed from what each agent actually spent.

    This function used to read `result.model_id`, `result.prompt_tokens` and
    `result.completion_tokens`. It is handed a `PipelineResult`, which has none
    of those fields — its type annotation said `ScanResult`, which was the tell.
    All three `getattr` calls fell through to their defaults, so it computed
    `compute_cost_usd("__default__", 0, 0)` = **0.0 on every scan**. The Result
    Dashboard rendered $0.00 as a "Cost" metric card for requests that had just
    made up to seven paid LLM calls, and the `cost_calc` span carried
    `llm.prompt_tokens: 0`, `llm.completion_tokens: 0`, `llm.cost_usd: 0` into
    SigNoz.

    The real per-call cost was always recorded correctly inside `_call_llm` ->
    `record_llm_observability`, so the aggregate counters were right while the
    per-request span and the API response said zero — two sources disagreeing,
    with the one a human reads being the wrong one.

    Cost is computed at the call site (`ai_agent._call_cost`) because that is the
    only place the prompt/completion split exists, and the two are priced
    differently — a total-token figure cannot be converted to cost after the fact.
    """
    agents = _agents_of(result)
    cost = _sum_cost(*agents)
    tokens = _sum_tokens(*agents)
    model_id = str(getattr(getattr(result, "scan", None), "model_used", None) or "unknown")

    span.set_attribute("llm.model_id", model_id)
    span.set_attribute("llm.usage_reported", tokens is not None)
    if tokens is not None:
        span.set_attribute("llm.total_tokens", tokens)
    if cost is not None:
        span.set_attribute("llm.cost_usd", cost)

    if cost is not None and telemetry.COST_PER_REQUEST_USD is not None:
        telemetry.COST_PER_REQUEST_USD.record(cost, {"model": model_id})
    if tokens is not None and telemetry.TOKENS_TOTAL is not None:
        telemetry.TOKENS_TOTAL.add(tokens, {"model": model_id})
    return cost


def _emit_request_metrics(result, latency_s: float, cache_hit: bool) -> None:
    """Latency always; throughput only when the tokens are actually known.

    `total_tokens` was read from `prompt_tokens`/`completion_tokens`, which a
    `PipelineResult` does not have, so it was always 0 and the
    `if total_tokens and ...` guard never passed — the
    `devguard.llm.tokens_per_sec` histogram documented in `telemetry.py` has
    never received a single observation.
    """
    if telemetry.REQUEST_LATENCY_MS is not None:
        telemetry.REQUEST_LATENCY_MS.record(latency_s * 1000, {"cache_hit": str(cache_hit)})

    total_tokens = _sum_tokens(*_agents_of(result))
    if total_tokens and latency_s > 0 and telemetry.TOKENS_PER_SEC is not None:
        telemetry.TOKENS_PER_SEC.record(total_tokens / latency_s)


def _extract_score(result) -> float:
    """Best-effort extraction of a 0-100 validator score from whatever shape
    `result` actually has."""
    for attr in ("eval_score", "score", "validator_score"):
        val = getattr(result, attr, None)
        if val is not None:
            return float(val)
    final_validation = getattr(result, "final_validation", None)
    if final_validation is not None:
        val = getattr(final_validation, "eval_score", None)
        if val is not None:
            return float(val)
    return 0.0


def _dump(result) -> dict[str, Any]:
    try:
        return result.model_dump()
    except AttributeError:
        return result.dict()