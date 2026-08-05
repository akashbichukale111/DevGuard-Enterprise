"""
ablation.py — the retrieval ablation's harness, and the cost-accounting requirement's cost accounting.

The retrieval ablation: *"Same incident, same asset. Two arms: `retrieval=on` and `retrieval=off`
(a flag). **N ≥ 5 runs per arm.** Report **median** plus min/max, not a single
number. Primary metric: **time-to-root-cause**. Secondary: MCP tool calls,
tokens, and cost per arm."*

READ THIS BEFORE READING ANY NUMBER THIS MODULE PRODUCES.

The ablation is designed to measure whether *retrieving DevGuard's own prior
runbooks* makes it reach a root cause faster. That effect is mediated entirely
by the Diagnostician: retrieved knowledge enters its prompt, and a better prompt
should shorten the reasoning. **The Diagnostician cannot run in this
environment** — `api.groq.com` is denied at CONNECT — so the mechanism the
ablation exists to measure is absent.

What this harness therefore measures is **the cost of retrieval, not its
benefit**: the extra MCP round trip, the extra evidence items, the extra
milliseconds. The benefit side is structurally zero here, and that is a fact
about the environment rather than a finding about retrieval. The retrieval ablation says a measured
null beats a fabricated win; this is weaker than a null — it is a *structurally
determined* zero, and the published results say so in those words rather than
implying an experiment that was not run.

The harness itself is real and complete. On a machine with a working key, the
same command produces the comparison the retrieval ablation actually asks for.

TWO TIMINGS, BOTH REPORTED (the measured-numbers rule — every number comes from a real clock):

  `time_to_root_cause_s`   the whole thing, detection included
  `post_detection_s`       from "failure observed" to "root cause available"

Both are published because either alone misleads. `dbt run` takes seconds and
dominates the total, which would bury any retrieval effect — the same reason the retrieval ablation
rejects MTTR in favour of time-to-root-cause. But dropping detection entirely
would understate what an operator actually waits for.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

#: Cost accounting. Populated from a real provider price list only when a real call is made.
#: Zero here because zero calls are made — not because the model is free.
USD_PER_1K_PROMPT_TOKENS = 0.0
USD_PER_1K_COMPLETION_TOKENS = 0.0


@dataclass
class PhaseTiming:
    name: str
    seconds: float
    tool_calls: int = 0


@dataclass
class RunMetrics:
    """One ablation run. Every field is measured, none is estimated."""

    arm: str
    run_index: int
    incident_id: str

    time_to_root_cause_s: float
    post_detection_s: float
    detection_s: float

    mcp_tool_calls: int
    evidence_items: int
    documents_retrieved: int

    tokens_prompt: int = 0
    tokens_completion: int = 0
    model_calls: int = 0
    usd_cost: float = 0.0

    root_cause_source: str = "DERIVED"
    """DERIVED (deterministic) or MODEL. Never blank, never guessed."""

    diagnostician_verdict: str = ""
    chain_is_sufficient: bool = False
    phases: list[PhaseTiming] = field(default_factory=list)
    proof_pack: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["phases"] = [asdict(p) for p in self.phases]
        return payload


class Stopwatch:
    """Monotonic phase timing. Rule: numbers are generated, never typed."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._marks: list[tuple[str, float]] = []
        self.phases: list[PhaseTiming] = []

    def mark(self, name: str, tool_calls: int = 0) -> float:
        now = time.monotonic()
        previous = self._marks[-1][1] if self._marks else self._t0
        self._marks.append((name, now))
        elapsed = now - previous
        self.phases.append(PhaseTiming(name=name, seconds=round(elapsed, 4),
                                       tool_calls=tool_calls))
        return elapsed

    def since_start(self) -> float:
        return time.monotonic() - self._t0

    def since(self, name: str) -> float:
        for mark_name, when in self._marks:
            if mark_name == name:
                return time.monotonic() - when
        raise KeyError(name)


@dataclass
class ArmSummary:
    """The retrieval ablation: median plus min/max. Never a single number."""

    arm: str
    n: int
    ttrc_median_s: float
    ttrc_min_s: float
    ttrc_max_s: float
    post_detection_median_s: float
    post_detection_min_s: float
    post_detection_max_s: float
    mcp_tool_calls_median: float
    evidence_items_median: float
    documents_retrieved_median: float
    tokens_total: int
    model_calls_total: int
    usd_cost_total: float

    @classmethod
    def of(cls, arm: str, runs: Sequence[RunMetrics]) -> "ArmSummary":
        if not runs:
            raise ValueError(f"no runs for arm {arm!r}")
        ttrc = [r.time_to_root_cause_s for r in runs]
        post = [r.post_detection_s for r in runs]
        return cls(
            arm=arm, n=len(runs),
            ttrc_median_s=round(statistics.median(ttrc), 4),
            ttrc_min_s=round(min(ttrc), 4), ttrc_max_s=round(max(ttrc), 4),
            post_detection_median_s=round(statistics.median(post), 4),
            post_detection_min_s=round(min(post), 4),
            post_detection_max_s=round(max(post), 4),
            mcp_tool_calls_median=statistics.median(r.mcp_tool_calls for r in runs),
            evidence_items_median=statistics.median(r.evidence_items for r in runs),
            documents_retrieved_median=statistics.median(
                r.documents_retrieved for r in runs),
            tokens_total=sum(r.tokens_prompt + r.tokens_completion for r in runs),
            model_calls_total=sum(r.model_calls for r in runs),
            usd_cost_total=round(sum(r.usd_cost for r in runs), 6),
        )


def usd_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Cost accounting. Returns 0.0 when no tokens were spent — because none were."""
    return round(
        prompt_tokens / 1000 * USD_PER_1K_PROMPT_TOKENS
        + completion_tokens / 1000 * USD_PER_1K_COMPLETION_TOKENS, 6)


def compare(on: ArmSummary, off: ArmSummary) -> dict:
    """The headline comparison, with an explicit interpretability flag.

    `retrieval_could_affect_root_cause` is the field that keeps this honest. It
    is False whenever no model ran, because in that case the two arms cannot
    differ in *reasoning* — only in the cost of fetching documents nobody read.
    A reader who sees a delta of ~0 must be able to tell "retrieval does not
    help" from "the thing retrieval helps was switched off".
    """
    delta_ttrc = round(on.ttrc_median_s - off.ttrc_median_s, 4)
    delta_post = round(on.post_detection_median_s - off.post_detection_median_s, 4)
    model_ran = (on.model_calls_total + off.model_calls_total) > 0
    return {
        "delta_ttrc_median_s": delta_ttrc,
        "delta_post_detection_median_s": delta_post,
        "delta_mcp_tool_calls_median": on.mcp_tool_calls_median - off.mcp_tool_calls_median,
        "delta_evidence_items_median": on.evidence_items_median - off.evidence_items_median,
        "retrieval_could_affect_root_cause": model_ran,
        "interpretation": (
            "Both arms produced the root cause the same way, so this delta is the "
            "COST of retrieval and says nothing about its benefit."
            if not model_ran else
            "Both arms reasoned with a model; this delta is the measured effect "
            "of retrieval on time-to-root-cause."),
    }


def write_timings(path: str | os.PathLike, runs: Sequence[RunMetrics],
                  summaries: dict[str, ArmSummary], comparison: dict) -> str:
    """The single source: every published number is rendered from this file."""
    payload = {
        "schema": "devguard.timings/1",
        "generated_by": "backend/v2/ablation.py",
        "arms": {name: asdict(summary) for name, summary in summaries.items()},
        "comparison": comparison,
        "cost_model": {
            "usd_per_1k_prompt_tokens": USD_PER_1K_PROMPT_TOKENS,
            "usd_per_1k_completion_tokens": USD_PER_1K_COMPLETION_TOKENS,
            "note": ("Zero because zero model calls were made in this environment, "
                     "not because inference is free. Populate from the provider's "
                     "price list when a key is available."),
        },
        "runs": [r.to_dict() for r in runs],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return str(target)
