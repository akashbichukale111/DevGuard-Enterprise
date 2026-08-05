"""
backend/core/local_telemetry.py

SELF-OBSERVATION FALLBACK: an in-memory, same-process shadow of LLM spend, used
because the SigNoz MCP server's wire protocol is unverified (see
`docs/MCP_DECISION.md`). This gives the self-observing router *real* numbers to
react to today rather than waiting on the MCP integration.

THIS IS NOT DECORATION. `self_observer.adaptive_select_model` compares this
module's 30-minute total against `COST_BUDGET_USD_PER_30MIN` to decide whether to
downgrade the Fixer's model. The numbers here gate a routing decision, so their
accuracy is load-bearing.

TWO DEFECTS THIS MODULE USED TO HAVE
------------------------------------
1. **It approximated when the exact figure was available.** Cost was
   `(len(text_in) + len(text_out)) / 4` tokens at a flat per-model price, and the
   docstring said "Swap in real `usage.total_tokens` from the Groq response later
   for exact figures." That became possible when `ai_agent._call_cost` landed —
   the provider's prompt/completion split sits in the same function that calls
   `record_llm_call`. Measured divergence on realistic agent calls:

       scanner: 6KB in, 1KB out      heuristic $0.001050  real $0.001082   -3.0%
       fixer:   6KB in, 6KB out      heuristic $0.001800  real $0.002070  -13.0%
       validator: 12KB in, 500B out  heuristic $0.001875  real $0.001869   +0.3%
       TOTAL over one pipeline run   heuristic $0.004770  real $0.005070   -5.9%

   It **under**-reported, so the budget gate fired later than it should and spend
   overshot the ceiling before conservation mode engaged — the wrong direction
   for a cost guard. And the error is not a constant offset that averages out: it
   tracks the prompt/completion ratio, because a flat price cannot be right for
   both when real pricing splits them.

2. **A second price table.** `PRICE_PER_1K_TOKENS_USD` here duplicated
   `telemetry.MODEL_PRICING_USD_PER_1M` in a different unit and a different
   shape. Update one and the other silently diverges, with the stale one gating
   the budget. There is now exactly one table, in `telemetry.py`, and this module
   defers to it — including its deliberate bias of pricing unknown models high
   "so unknown spend is never *under*-reported."

The char/4 estimate remains as the fallback for calls with no reported usage
(streaming, or an SDK that omits `usage`), and a window containing any estimate
reports `exact: False` so a decision made on approximations can say so.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, NamedTuple, Optional

from backend.core.telemetry import compute_cost_usd

#: Characters per token for the fallback estimate. A standard rough heuristic;
#: only used when the provider reported no usage.
_CHARS_PER_TOKEN = 4

#: Bounded on purpose — a long-running process must not grow here.
_MAX_SAMPLES = 5000


class _Sample(NamedTuple):
    ts: float
    model: str
    cost_usd: float
    #: False when `cost_usd` came from the char/4 fallback rather than from
    #: provider-reported usage. Kept per sample so a window can report whether
    #: any part of its total is approximate.
    exact: bool


_lock = threading.Lock()
_samples: Deque[_Sample] = deque(maxlen=_MAX_SAMPLES)


def reset() -> None:
    """Drop all samples. For tests; never called in the request path."""
    with _lock:
        _samples.clear()


def estimate_cost_usd(model: str, text_in: str, text_out: str) -> float:
    """Fallback cost estimate for a call with no provider-reported usage.

    Prices through `telemetry.compute_cost_usd` so prompt and completion are
    charged at their real, different rates and there is only one price table to
    maintain. Token counts are still approximate — that is what makes this the
    fallback — but the pricing is not.
    """
    prompt_tokens = int(len(text_in) / _CHARS_PER_TOKEN)
    completion_tokens = int(len(text_out) / _CHARS_PER_TOKEN)
    return compute_cost_usd(model, prompt_tokens, completion_tokens)


def record_llm_call(
    model: str,
    text_in: str,
    text_out: str,
    cost_usd: Optional[float] = None,
    ts: Optional[float] = None,
) -> float:
    """Record one call's spend. Pass `cost_usd` whenever the real figure is known.

    `cost_usd` comes from `ai_agent._call_cost`, computed from the provider's
    prompt/completion split. When it is None the char/4 estimate is used and the
    sample is flagged inexact.
    """
    exact = cost_usd is not None
    cost = cost_usd if exact else estimate_cost_usd(model, text_in, text_out)
    with _lock:
        _samples.append(
            _Sample(
                ts=ts if ts is not None else time.time(),
                model=model,
                cost_usd=cost,
                exact=exact,
            )
        )
    return cost


def get_recent_cost_trend_local(window_minutes: int = 30) -> dict:
    """Spend over the recent window, with provenance.

    `exact` is False if ANY sample in the window was estimated, so a caller
    deciding on this total knows whether it is measured or approximated.
    """
    cutoff = time.time() - window_minutes * 60
    with _lock:
        recent = [s for s in _samples if s.ts >= cutoff]
    total = sum(s.cost_usd for s in recent)
    count = len(recent)
    estimated = sum(1 for s in recent if not s.exact)
    by_model: dict[str, float] = {}
    for s in recent:
        by_model[s.model] = by_model.get(s.model, 0.0) + s.cost_usd
    return {
        "total_cost_usd": total,
        "avg_cost_per_request_usd": (total / count) if count else 0.0,
        "request_count": count,
        "cost_by_model": by_model,
        "estimated_samples": estimated,
        "exact": estimated == 0,
    }
