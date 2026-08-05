"""
backend/api/god_mode_simulators.py
===================================
"God Mode" UI support — simulation endpoints for the operations demo.

This file is 100% ADDITIVE. It does not import from, modify, or touch
`backend/api/router.py`, `backend/main.py`'s existing routes, or any
existing agent/telemetry/resilience code directly — it only calls into
`backend/core/god_mode_orchestrator.py`, which is itself an additive layer
on top of the existing `ai_agent.py` / `self_observer.py`. It defines its
own `APIRouter` with its own prefix so there is zero risk of route
collisions with the existing pipeline endpoints (/scan, /slo-status,
/audit-log, etc.).

Each endpoint now calls the REAL orchestrator function for its module.
Those orchestrator functions are themselves fail-safe (see
god_mode_orchestrator.py's docstring): if a live pipeline/telemetry call
isn't available, they degrade to a realistic synthetic response rather
than raising. The `payload` bodies below are still accepted so the
frontend can pass tuning knobs (severity, intensity, code, etc.) straight
through to the orchestrator where relevant.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.god_mode_orchestrator import (
    execute_finops_agent,
    execute_llm_judge,
    execute_omni_heal,
    execute_precog_agent,
    generate_executive_summary,
)

# Own prefix + tag — keeps this fully namespaced away from the real API.
god_mode_router = APIRouter(prefix="/god-mode", tags=["god-mode-simulators"])


# ---------------------------------------------------------------------------
# Request bodies (all fields optional so the frontend can call these with
# an empty body during early UI development, or supply real inputs — e.g.
# `code` — to drive the underlying orchestrator function in "live" mode).
# ---------------------------------------------------------------------------
class ErrorSimRequest(BaseModel):
    error_rate_pct: Optional[float] = 35.0
    duration_s: Optional[int] = 60
    code: Optional[str] = None  # if given, execute_omni_heal() runs the real pipeline on it


class CostSpikeSimRequest(BaseModel):
    multiplier: Optional[float] = 4.0
    window_minutes: Optional[int] = 30


class MemoryLeakSimRequest(BaseModel):
    growth_mb_per_min: Optional[float] = 12.5
    duration_minutes: Optional[int] = 20


class HallucinationSimRequest(BaseModel):
    cwe_id: Optional[str] = "CWE-89"
    confidence_floor: Optional[float] = 0.4
    code: Optional[str] = None  # if given, execute_llm_judge() runs the real scanner on it


class GodModeSimRequest(BaseModel):
    scenario: Optional[str] = "full_chaos"
    intensity: Optional[float] = 0.7  # 0.0 - 1.0
    include_omni_heal: Optional[bool] = True
    include_finops: Optional[bool] = True
    include_precog: Optional[bool] = True
    include_truth_serum: Optional[bool] = True


# ===========================================================================
# 1. POST /god-mode/simulate/error
#    Mimics a burst of pipeline/LLM errors tripping the circuit breaker.
#    Delegates to the real Omni-Heal orchestrator function.
# ===========================================================================
@god_mode_router.post("/simulate/error")
async def simulate_error(payload: ErrorSimRequest = ErrorSimRequest()):
    result = await execute_omni_heal(code=payload.code)
    return result


# ===========================================================================
# 2. POST /god-mode/simulate/cost-spike
#    Mimics a sudden LLM spend spike that should trigger cost-aware routing.
#    Delegates to the real FinOps orchestrator function.
# ===========================================================================
@god_mode_router.post("/simulate/cost-spike")
async def simulate_cost_spike(payload: CostSpikeSimRequest = CostSpikeSimRequest()):
    result = await execute_finops_agent()
    return result


# ===========================================================================
# 3. POST /god-mode/simulate/memory-leak
#    Mimics a slow memory-growth incident (e.g. Redis client / span buffer).
#    Delegates to the real Pre-Cog Ops orchestrator function.
# ===========================================================================
@god_mode_router.post("/simulate/memory-leak")
async def simulate_memory_leak(payload: MemoryLeakSimRequest = MemoryLeakSimRequest()):
    result = await execute_precog_agent()
    return result


# ===========================================================================
# 4. POST /god-mode/simulate/hallucination
#    Mimics the Scanner Agent producing a low-confidence / fabricated finding.
#    Delegates to the real Truth Serum orchestrator function.
# ===========================================================================
@god_mode_router.post("/simulate/hallucination")
async def simulate_hallucination(payload: HallucinationSimRequest = HallucinationSimRequest()):
    result = await execute_llm_judge(code=payload.code, cwe_id=payload.cwe_id)
    return result


# ===========================================================================
# 5. POST /god-mode/simulate/god-mode
#    The "everything at once" scenario — combines all four modules into the
#    Executive SRE Commander's summary for the dashboard's headline view.
# ===========================================================================
@god_mode_router.post("/simulate/god-mode")
async def simulate_god_mode(payload: GodModeSimRequest = GodModeSimRequest()):
    result = await generate_executive_summary(
        include_omni_heal=payload.include_omni_heal,
        include_finops=payload.include_finops,
        include_precog=payload.include_precog,
        include_truth_serum=payload.include_truth_serum,
    )
    return result
