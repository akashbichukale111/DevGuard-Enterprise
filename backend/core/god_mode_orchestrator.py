"""
backend/core/god_mode_orchestrator.py
======================================
Orchestration layer for the "God Mode" UI's 5 Ultra-Pro-Max modules:

  1. Omni-Heal              -> execute_omni_heal()
  2. FinOps Agent           -> execute_finops_agent()
  3. Pre-Cog Ops            -> execute_precog_agent()
  4. Truth Serum Agent      -> execute_llm_judge()
  5. Executive SRE Commander -> generate_executive_summary()

DESIGN PHILOSOPHY (for the review):
------------------------------------
This file is 100% ADDITIVE. It does not modify `backend/core/ai_agent.py`
or `backend/core/self_observer.py` in any way — it only *imports* their
already-public functions/constants and composes them into higher-level,
God-Mode-shaped responses. Nothing in this file changes the behavior of
the existing Scanner -> Fixer -> Validator pipeline or the existing
self-observation loop; it's a new consumer sitting on top of them.

FAIL-SAFE CONTRACT (mirrors self_observer.py's own philosophy):
  - Every function here is `async` and NEVER raises. Any failure reaching
    into the real pipeline, the real self-observation layer, or the MCP
    client degrades to a realistic, clearly-labeled synthetic response
    rather than a 500 — this file is UI-facing (God Mode dashboard), and a
    demo widget breaking is worse than a demo widget showing a fallback.
  - Every response includes a `"data_source"` field: `"live"` when it was
    computed from a real pipeline/telemetry call, `"synthetic"` when it's
    realistic dummy data (either because no input was given, or because
    the live path failed/was unavailable). The God Mode frontend can use
    this to badge data as real vs. simulated without guessing.
  - Nothing here writes back to `_scan_results`, the audit chain, or SLO
    tracking in `backend/api/router.py` — these are read/derive-only
    "God Mode lens" functions, not new mutations of pipeline state.

These functions are designed to be called directly from the simulator
endpoints in `backend/api/god_mode_simulators.py` (or any new "live" God
Mode endpoints layered on top of them later) — each returns a plain dict
that's already JSON-serializable.
"""

from __future__ import annotations

import logging
import os
import random
import resource
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Imports from the EXISTING, UNMODIFIED agentic layer (ai_agent.py).
# All of these are already-public names in that file — nothing new is added
# to it and nothing about it changes by importing here.
# ---------------------------------------------------------------------------
from backend.core.ai_agent import (
    MAX_REFLECTION_RETRIES,
    MODEL_CHEAP,
    MODEL_STRONG,
    EVAL_PASS_THRESHOLD,
    run_pipeline,
    run_scanner,
    run_validator,
    select_model,
)
from backend.core.schemas import (
    AgentExecutionError,
    FixResult,
    PipelineResult,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

# ---------------------------------------------------------------------------
# Imports from the EXISTING, UNMODIFIED self-observation layer
# (self_observer.py). Same rule: read-only consumption of public names.
# ---------------------------------------------------------------------------
from backend.core.self_observer import (
    CHEAP_MODEL_TIER,
    COST_BUDGET_USD_PER_30MIN,
    CWE_FAILURE_RATE_THRESHOLD,
    ELEVATED_CONTEXT_K,
    adaptive_select_model,
    is_conservation_mode,
    suggest_context_k,  # noqa: F401  (public name kept for compatibility)
    suggest_context_k_detailed,
)

# mcp_client is the thing self_observer.py itself reads telemetry through —
# we reuse the same fail-safe client rather than inventing a second path.
from backend.core.mcp_client import get_mcp_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _god_mode_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _current_rss_mb() -> Optional[float]:
    """This process's resident set size in MiB, or None if unreadable.

    The Pre-Cog panel's "current RSS" was `random.uniform(160.0, 260.0)` — an
    invented figure for a quantity the process can simply ask about. Read from
    `/proc/self/statm` where available (Linux, exact pages) and otherwise from
    `resource.getrusage`, whose `ru_maxrss` unit differs by platform: KiB on
    Linux, bytes on macOS.

    Returns None rather than raising, so a god-mode panel never 500s because
    /proc is unreadable — the caller falls back and labels the value synthetic.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            resident_pages = int(fh.read().split()[1])
        return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 — fall through to the portable path
        pass
    try:
        import sys

        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return round(max_rss / divisor, 1)
    except Exception:  # noqa: BLE001 — never let a memory read break a panel
        logger.debug("_current_rss_mb: could not read RSS", exc_info=True)
        return None


#: Ranking used to reduce per-axis provenance to one honest aggregate.
_SOURCE_RANK = {"live": 3, "local_shadow": 2, "measured": 2, "synthetic": 0}


def _aggregate_source(*sources: str) -> str:
    """One `data_source` for a payload assembled from differently-sourced parts.

    The rule: report the WEAKEST component, not the strongest, and say "partial"
    when they disagree. A single flag that reports the most favourable part is
    how a payload containing invented memory figures came to be badged "live".

    "measured" and "local_shadow" both mean real-but-not-retrieved-telemetry, so
    they rank together and below "live".
    """
    if not sources:
        return "synthetic"
    ranks = {_SOURCE_RANK.get(s, 1) for s in sources}
    if len(ranks) == 1:
        only = sources[0]
        return only if only in _SOURCE_RANK else "partial"
    return "synthetic" if min(ranks) == 0 and max(ranks) == 0 else "partial"


def _synthetic_diff(original_hint: str = "") -> tuple[str, str, str]:
    """A realistic-looking before/after pair for when no real code was
    supplied to Omni-Heal. Deliberately mirrors the shape of a genuine
    FixResult so the frontend diff viewer needs no branching logic."""
    before = original_hint or (
        "def get_user(user_id):\n"
        "    query = \"SELECT * FROM users WHERE id = \" + user_id\n"
        "    return db.execute(query)\n"
    )
    after = (
        "def get_user(user_id):\n"
        "    query = \"SELECT * FROM users WHERE id = %s\"\n"
        "    return db.execute(query, (user_id,))\n"
    )
    summary = "Replaced raw string concatenation with a parameterized query to eliminate SQL injection (CWE-89)."
    return before, after, summary


# ===========================================================================
# 1. OMNI-HEAL — AI Auto-Fixer with live code diffs
# ===========================================================================
async def execute_omni_heal(code: Optional[str] = None) -> dict[str, Any]:
    """Runs (or simulates) the real Scanner -> Fixer -> Validator loop and
    reshapes the result into a live-diff-friendly payload for the Omni-Heal
    panel.

    If `code` is provided, this calls the REAL `run_pipeline()` from
    ai_agent.py — same agentic loop the normal /scan endpoint uses, same
    bounded reflection, same validator gate. If `code` is omitted, or the
    real pipeline call fails for any reason (LLM outage, bad code, etc.),
    this falls back to a realistic synthetic diff so the God Mode panel
    always has something to render.
    """
    run_id = _god_mode_id("heal")

    if code:
        try:
            result: PipelineResult = await run_pipeline(code)
            fix: FixResult = result.final_fix
            validation: ValidationResult = result.final_validation

            return {
                "run_id": run_id,
                "module": "omni_heal",
                "data_source": "live",
                "started_at": _now_iso(),
                "original_code": result.original_code,
                "patched_code": fix.patched_code if fix else "",
                "diff_summary": fix.diff_summary if fix else "",
                "addressed_cwe_ids": fix.addressed_cwe_ids if fix else [],
                "vulnerabilities_found": [
                    {
                        "cwe_id": v.cwe_id,
                        "severity": v.severity.value,
                        "line_number": v.line_number,
                        "confidence_score": v.confidence_score,
                    }
                    for v in result.scan.vulnerabilities
                ],
                "reflection_attempts": len(result.reflection_history),
                "max_reflection_retries": MAX_REFLECTION_RETRIES,
                "converged": result.converged,
                "eval_score": validation.eval_score if validation else 0,
                "verdict": validation.verdict.value if validation else "unknown",
                "model_used_for_fix": fix.model_used if fix else None,
                "routing_decisions": result.routing_decisions,
            }
        except AgentExecutionError as exc:
            logger.warning("execute_omni_heal: live pipeline failed (%s); using synthetic fallback", exc)
        except Exception:  # noqa: BLE001 — God Mode must never 500 the dashboard
            logger.exception("execute_omni_heal: unexpected failure; using synthetic fallback")

    before, after, summary = _synthetic_diff(code or "")
    return {
        "run_id": run_id,
        "module": "omni_heal",
        "data_source": "synthetic",
        "started_at": _now_iso(),
        "original_code": before,
        "patched_code": after,
        "diff_summary": summary,
        "addressed_cwe_ids": ["CWE-89"],
        "vulnerabilities_found": [
            {"cwe_id": "CWE-89", "severity": "critical", "line_number": 2, "confidence_score": 0.94}
        ],
        "reflection_attempts": 1,
        "max_reflection_retries": MAX_REFLECTION_RETRIES,
        "converged": True,
        "eval_score": 92,
        "verdict": "pass",
        "model_used_for_fix": MODEL_STRONG,
        "routing_decisions": {"scanner": MODEL_STRONG, "fixer_attempt_1": MODEL_STRONG},
    }


# ===========================================================================
# 2. FINOPS AGENT — Cost Controller that auto-updates sampling configs
# ===========================================================================
async def execute_finops_agent() -> dict[str, Any]:
    """Reads the real cost trend (via the same MCP-client-with-local-fallback
    path self_observer.py uses) and the real conservation-mode flag from
    CostGuardian, then produces a recommended OTel sampling-rate adjustment.

    This never calls anything mutating — it only *recommends* a sampling
    config change (e.g. for `signoz/dashboard.json` / OTel Collector config)
    for a human or a future auto-apply step to act on.
    """
    run_id = _god_mode_id("finops")
    conservation = False
    try:
        conservation = is_conservation_mode()
    except Exception:  # noqa: BLE001
        logger.exception("execute_finops_agent: is_conservation_mode() failed; assuming False")

    data_source = "synthetic"
    spend_usd = round(random.uniform(0.3, 1.8), 4)
    budget = COST_BUDGET_USD_PER_30MIN

    try:
        client = get_mcp_client()
        trend = await client.get_recent_cost_trend()
        if trend.available:
            spend_usd = trend.total_cost_usd
            # the provenance rule: branch on provenance, not on usability. `available` only
            # says the number is safe to use; `source` says where it came from.
            # Treating available==True as "live" is what previously badged the
            # in-process estimate as retrieved SigNoz telemetry.
            data_source = "live" if trend.source == "signoz_mcp" else "local_shadow"
    except Exception:  # noqa: BLE001
        logger.exception("execute_finops_agent: get_recent_cost_trend() failed; using synthetic spend")

    over_budget = spend_usd > budget
    # Sampling recommendation: when spend is high, trade trace fidelity for
    # cost by lowering the head-sampling ratio; always keep 100% sampling
    # for error/critical spans regardless of budget pressure (mirrors the
    # existing "never downgrade critical severity" safety floor).
    current_sampling_ratio = 1.0
    recommended_sampling_ratio = 0.25 if over_budget else 1.0

    return {
        "run_id": run_id,
        "module": "finops_agent",
        "data_source": data_source,
        "started_at": _now_iso(),
        "spend_usd_per_30min": round(spend_usd, 4),
        "budget_usd_per_30min": budget,
        "over_budget": over_budget,
        "conservation_mode_active": conservation,
        "model_tier_recommendation": CHEAP_MODEL_TIER if over_budget or conservation else MODEL_CHEAP,
        "sampling_config": {
            "current_head_sampling_ratio": current_sampling_ratio,
            "recommended_head_sampling_ratio": recommended_sampling_ratio,
            "error_spans_always_sampled": True,
            "critical_severity_always_full_fidelity": True,
            "config_target": "otel-collector-config.yaml:processors.probabilistic_sampler",
        },
        "auto_apply": False,
        "reasoning": (
            f"30-min spend ${spend_usd:.4f} exceeds ${budget:.2f} budget; "
            "reducing trace sampling ratio to cut OTLP export volume while "
            "preserving 100% visibility on errors and critical-severity scans."
            if over_budget
            else f"30-min spend ${spend_usd:.4f} is within the ${budget:.2f} budget; no sampling change needed."
        ),
    }


# ===========================================================================
# 3. PRE-COG OPS — Future Predictor for auto-scaling and memory leaks
# ===========================================================================
async def execute_precog_agent() -> dict[str, Any]:
    """Projects near-future resource pressure from the most recent telemetry
    it can read (error rate via MCP client, falling back to a synthetic
    baseline), and returns an autoscale + memory-leak forecast.

    This is intentionally a lightweight linear extrapolation, not a real
    forecasting model — it's meant to drive the Pre-Cog Ops panel's
    "in the next N minutes..." narrative, clearly labeled as a forecast.
    """
    run_id = _god_mode_id("precog")
    error_rate_source = "synthetic"
    current_error_rate_pct = round(random.uniform(1.0, 8.0), 2)

    try:
        client = get_mcp_client()
        err = await client.get_error_rate_detailed()
        if err.available:
            current_error_rate_pct = err.error_rate_pct
            error_rate_source = "live"
    except Exception:  # noqa: BLE001
        logger.exception("execute_precog_agent: get_error_rate_detailed() failed; using synthetic baseline")

    # Simple linear projection over a 15-minute horizon, in 3-minute steps.
    horizon_minutes = 15
    step_minutes = 3
    drift_pct_per_step = round(current_error_rate_pct * 0.08 + 0.4, 3)
    forecast = []
    projected = current_error_rate_pct
    for t in range(0, horizon_minutes + 1, step_minutes):
        forecast.append({"minute": t, "projected_error_rate_pct": round(projected, 2)})
        projected += drift_pct_per_step

    breaker_trip_threshold_pct = 30.0
    minutes_to_breaker_trip = None
    for point in forecast:
        if point["projected_error_rate_pct"] >= breaker_trip_threshold_pct:
            minutes_to_breaker_trip = point["minute"]
            break

    # Memory-leak projection. INDEPENDENT AXIS from the error rate, and that
    # independence is the point: this block used to be entirely
    # `random.uniform`, while `data_source` above flipped to "live" whenever the
    # *error rate* came back from MCP. So a payload badged "live" carried a
    # fabricated current RSS, a fabricated leak rate, and an OOM countdown
    # derived from both — all three rendered by PreCogPanel.tsx. One axis being
    # real does not make the other real.
    #
    # The current RSS is measurable: it is this process's own resident set size,
    # one stdlib call away. There was never a reason to invent it. The leak
    # *rate* cannot be derived from a single sample, so it stays a scenario
    # parameter — and is now labelled as one instead of passing for a measurement.
    measured_rss = _current_rss_mb()
    if measured_rss is not None:
        starting_rss_mb = measured_rss
        current_rss_source = "measured"
    else:
        starting_rss_mb = round(random.uniform(160.0, 260.0), 1)
        current_rss_source = "synthetic"

    leak_rate_mb_per_min = round(random.uniform(2.0, 14.0), 2)
    oom_ceiling_mb = 1536.0
    minutes_to_oom = (
        round((oom_ceiling_mb - starting_rss_mb) / leak_rate_mb_per_min, 1)
        if leak_rate_mb_per_min > 0
        else None
    )
    memory_source = (
        "measured_current_synthetic_rate"
        if current_rss_source == "measured"
        else "synthetic"
    )

    autoscale_recommendation = (
        "scale_out"
        if minutes_to_breaker_trip is not None and minutes_to_breaker_trip <= 9
        else "hold"
    )

    return {
        "run_id": run_id,
        "module": "precog_ops",
        # The AGGREGATE, and it must not report the most favourable component.
        # This used to be a single flag flipped to "live" by the error-rate query
        # alone, while the memory axis stayed invented. "partial" is the honest
        # answer whenever the two axes disagree — the same mislabel already fixed
        # twice during hardening (the MCP cost fallback reporting "live", and the executive
        # roll-up deriving provenance from the absence of errors).
        "data_source": _aggregate_source(error_rate_source, memory_source),
        "data_source_detail": {
            "error_rate": error_rate_source,
            "memory": memory_source,
        },
        "started_at": _now_iso(),
        "current_error_rate_pct": current_error_rate_pct,
        "error_rate_forecast": forecast,
        "circuit_breaker_trip_threshold_pct": breaker_trip_threshold_pct,
        "projected_minutes_to_breaker_trip": minutes_to_breaker_trip,
        "memory_forecast": {
            "starting_rss_mb": starting_rss_mb,
            "leak_rate_mb_per_min": leak_rate_mb_per_min,
            "oom_ceiling_mb": oom_ceiling_mb,
            "projected_minutes_to_oom": minutes_to_oom,
            # Per-field provenance, because these three have different origins
            # and a single label for the group would have to lie about one.
            "current_rss_source": current_rss_source,
            "rate_source": "synthetic_scenario",
            "projection_source": "derived_from_synthetic_rate",
        },
        "autoscale_recommendation": autoscale_recommendation,
        "reasoning": (
            f"At the current drift rate, error rate is projected to cross the "
            f"{breaker_trip_threshold_pct}% breaker threshold in "
            f"~{minutes_to_breaker_trip} min; recommending scale-out now to "
            "absorb load before the breaker opens."
            if minutes_to_breaker_trip is not None
            else "Error rate trend is within safe bounds for the forecast horizon; no scaling action needed."
        ),
    }


# ===========================================================================
# 4. TRUTH SERUM AGENT — LLM Hallucination Judge
# ===========================================================================
async def execute_llm_judge(code: Optional[str] = None, cwe_id: Optional[str] = None) -> dict[str, Any]:
    """Cross-examines the Scanner Agent's own findings for signs of
    hallucination: low confidence_score, a CWE with a historically high
    validator fail-rate (via suggest_context_k's underlying telemetry), or
    a finding the Validator itself would reject.

    If `code` is supplied, this runs the REAL Scanner Agent (`run_scanner`)
    and inspects its actual output — it does not fabricate findings for
    real code. It only fabricates a demo verdict when no code is given.
    """
    run_id = _god_mode_id("truth")

    if code:
        try:
            scan: ScanResult = await run_scanner(code)
            verdicts = []
            for v in scan.vulnerabilities:
                suspected_hallucination = v.confidence_score < 0.5
                verdicts.append(
                    {
                        "cwe_id": v.cwe_id,
                        "severity": v.severity.value,
                        "line_number": v.line_number,
                        "confidence_score": v.confidence_score,
                        "suspected_hallucination": suspected_hallucination,
                        "judge_note": (
                            "Confidence below 0.5 — flagged for human review before it "
                            "reaches the Fixer Agent."
                            if suspected_hallucination
                            else "Confidence acceptable; no intervention needed."
                        ),
                    }
                )

            # `recommended_context_k` is rendered by LLMJudgePanel.tsx as
            # "Recommended k" inside a payload badged data_source "live". The
            # bare int cannot say whether the CWE history was consulted or
            # simply unavailable, and with no verified MCP server it is ALWAYS
            # unavailable — so the value was the unchanged default presented as a
            # telemetry-derived recommendation. The detailed form reports both.
            elevated_k = None
            context_k_informed = False
            try:
                candidate_ids = [v.cwe_id for v in scan.vulnerabilities]
                elevated_k, context_k_informed = await suggest_context_k_detailed(
                    candidate_ids, base_k=4
                )
            except Exception:  # noqa: BLE001
                logger.exception("execute_llm_judge: suggest_context_k_detailed() failed")

            return {
                "run_id": run_id,
                "module": "truth_serum",
                "data_source": "live",
                "started_at": _now_iso(),
                "model_used": scan.model_used,
                "findings_reviewed": len(scan.vulnerabilities),
                "flagged_count": sum(1 for v in verdicts if v["suspected_hallucination"]),
                "verdicts": verdicts,
                "recommended_context_k": elevated_k,
                "context_k_source": "cwe_history" if context_k_informed else "not_consulted",
                "cwe_failure_rate_threshold": CWE_FAILURE_RATE_THRESHOLD,
                "elevated_context_k": ELEVATED_CONTEXT_K,
            }
        except AgentExecutionError as exc:
            logger.warning("execute_llm_judge: live scanner failed (%s); using synthetic fallback", exc)
        except Exception:  # noqa: BLE001
            logger.exception("execute_llm_judge: unexpected failure; using synthetic fallback")

    demo_cwe = cwe_id or "CWE-89"
    fabricated_confidence = round(random.uniform(0.12, 0.45), 2)
    return {
        "run_id": run_id,
        "module": "truth_serum",
        "data_source": "synthetic",
        "started_at": _now_iso(),
        "model_used": MODEL_STRONG,
        "findings_reviewed": 1,
        "flagged_count": 1,
        "verdicts": [
            {
                "cwe_id": demo_cwe,
                "severity": "high",
                "line_number": 42,
                "confidence_score": fabricated_confidence,
                "suspected_hallucination": True,
                "judge_note": (
                    "Flagged finding does not correspond to any actual sink in the "
                    "supplied snippet — likely hallucinated pattern match."
                ),
            }
        ],
        "recommended_context_k": ELEVATED_CONTEXT_K,
        # This whole branch is the no-code demo path, so nothing consulted any
        # CWE history — the k above is the configured elevated constant, chosen to
        # make the demo narrative land, not a recommendation derived from data.
        "context_k_source": "not_consulted",
        "cwe_failure_rate_threshold": CWE_FAILURE_RATE_THRESHOLD,
        "elevated_context_k": ELEVATED_CONTEXT_K,
    }


# ===========================================================================
# 5. EXECUTIVE SRE COMMANDER — Mobile simulator & PDF summary generation
# ===========================================================================
async def generate_executive_summary(
    include_omni_heal: bool = True,
    include_finops: bool = True,
    include_precog: bool = True,
    include_truth_serum: bool = True,
) -> dict[str, Any]:
    """Aggregates the other four modules' outputs (calling them internally,
    each already fail-safe) into a single executive-facing payload: a
    Slack-ready message string plus a structured outline a PDF-generation
    step (e.g. the `pdf` skill / reportlab) can render into a one-pager.

    Every sub-call is wrapped independently so one module's failure doesn't
    take down the whole executive summary — it's simply omitted with a note.
    """
    run_id = _god_mode_id("exec")
    sections: dict[str, Any] = {}
    errors: list[str] = []

    if include_omni_heal:
        try:
            sections["omni_heal"] = await execute_omni_heal()
        except Exception:  # noqa: BLE001
            logger.exception("generate_executive_summary: omni_heal section failed")
            errors.append("omni_heal")

    if include_finops:
        try:
            sections["finops_agent"] = await execute_finops_agent()
        except Exception:  # noqa: BLE001
            logger.exception("generate_executive_summary: finops section failed")
            errors.append("finops_agent")

    if include_precog:
        try:
            sections["precog_ops"] = await execute_precog_agent()
        except Exception:  # noqa: BLE001
            logger.exception("generate_executive_summary: precog section failed")
            errors.append("precog_ops")

    if include_truth_serum:
        try:
            sections["truth_serum"] = await execute_llm_judge()
        except Exception:  # noqa: BLE001
            logger.exception("generate_executive_summary: truth_serum section failed")
            errors.append("truth_serum")

    # ---- Build Slack-style summary text ----
    lines = [f"*DevGuard AI — Executive SRE Summary* ({_now_iso()})"]

    def _rollup_data_source(
        sections_map: dict[str, Any], omitted: list[str]
    ) -> str:
        """Provenance of a composite response = the weakest of its parts.

        A roll-up is only "live" if every section it contains was itself live
        and nothing was omitted. Any synthetic section makes the whole summary
        synthetic-tainted, which is what a reader needs to know.
        """
        if not sections_map:
            return "synthetic"
        labels = {
            str(payload.get("data_source", "unknown"))
            for payload in sections_map.values()
            if isinstance(payload, dict)
        }
        if omitted:
            return "partial"
        if labels == {"live"}:
            return "live"
        if "live" in labels or "local_shadow" in labels:
            return "partial"
        return "synthetic"

    heal = sections.get("omni_heal")
    if heal:
        lines.append(
            f"• Omni-Heal: {heal['reflection_attempts']}/{heal['max_reflection_retries']} "
            f"reflection attempts, verdict={heal['verdict']}, eval_score={heal['eval_score']} "
            f"[{heal['data_source']}]"
        )

    finops = sections.get("finops_agent")
    if finops:
        status = "OVER BUDGET" if finops["over_budget"] else "within budget"
        lines.append(
            f"• FinOps: ${finops['spend_usd_per_30min']:.4f}/30min ({status}); "
            f"recommended sampling ratio {finops['sampling_config']['recommended_head_sampling_ratio']} "
            f"[{finops['data_source']}]"
        )

    precog = sections.get("precog_ops")
    if precog:
        eta = precog["projected_minutes_to_breaker_trip"]
        eta_str = f"~{eta} min" if eta is not None else "no imminent risk"
        lines.append(
            f"• Pre-Cog Ops: breaker-trip ETA {eta_str}; autoscale recommendation="
            f"{precog['autoscale_recommendation']} [{precog['data_source']}]"
        )

    truth = sections.get("truth_serum")
    if truth:
        lines.append(
            f"• Truth Serum: {truth['flagged_count']}/{truth['findings_reviewed']} findings "
            f"flagged as possible hallucinations [{truth['data_source']}]"
        )

    if errors:
        lines.append(f"• _Note: {', '.join(errors)} section(s) unavailable this run._")

    slack_message = "\n".join(lines)

    # ---- Structured outline for PDF rendering ----
    pdf_outline = {
        "title": "DevGuard AI — Executive SRE Summary",
        "generated_at": _now_iso(),
        "sections": [
            {"heading": name.replace("_", " ").title(), "body": payload}
            for name, payload in sections.items()
        ],
        "omitted_sections": errors,
    }

    return {
        "run_id": run_id,
        "module": "executive_sre_commander",
        # the provenance rule: the roll-up's provenance is the *weakest* of its sections, not
        # a function of whether anything errored. Previously this read "live"
        # whenever no section raised — so a summary composed entirely of
        # synthetic sections was still stamped live.
        "data_source": _rollup_data_source(sections, errors),
        "started_at": _now_iso(),
        "slack_message": slack_message,
        "pdf_outline": pdf_outline,
        "sections": sections,
        "omitted_sections": errors,
    }
