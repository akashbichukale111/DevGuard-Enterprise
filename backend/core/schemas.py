"""
schemas.py — Strict Pydantic v2 contracts for the DevGuard AI agentic layer.

DESIGN PHILOSOPHY (for the review):
-----------------------------------
Every agent boundary in this system is a *contract*. The whole point of a
multi-agent pipeline is that each agent's output is the next agent's input,
so if outputs are loosely-typed free text, errors compound silently across
hops. We therefore validate EVERY inter-agent message against a Pydantic
model. This gives us three things for free:

  1. Fail-fast: a malformed LLM response is caught at the boundary, not three
     agents downstream where it's undebuggable.
  2. Self-documentation: the schema *is* the spec the LLM is prompted against
     (we serialize model_json_schema() into the system prompts).
  3. Observability: the telemetry module can attach these models as span
     attributes without any custom serialization glue.

We deliberately keep enums for severity/verdict so routing logic and the
reflection loop can branch on typed values instead of stringly-typed guesses.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.languages import DEFAULT_LANGUAGE, resolve


# ---------------------------------------------------------------------------
# Enums — typed vocabularies shared across agents
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """
    CVSS-inspired severity bands. We use these both for reporting AND for
    model routing (see select_model). Keeping them as an enum means the router
    can never receive an unexpected string — a malformed LLM severity is
    rejected at the schema boundary before it can influence a routing cost
    decision.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Verdict(str, Enum):
    """Validator/Critic pass-fail outcome."""
    PASS = "pass"
    FAIL = "fail"


# ---------------------------------------------------------------------------
# Scanner Agent output
# ---------------------------------------------------------------------------

class Vulnerability(BaseModel):
    """
    A single finding from the Scanner Agent.

    `confidence_score` is model-reported and later used for calibration
    metrics in the benchmark. We do NOT trust it blindly — it's a signal, not
    a guarantee — but exposing it lets the frontend surface uncertainty and
    lets us measure calibration (are 0.9-confidence findings actually right
    90% of the time?).
    """
    cwe_id: str = Field(
        ...,
        description="CWE identifier, e.g. 'CWE-89'. Must be of the form CWE-<n>.",
        examples=["CWE-89"],
    )
    severity: Severity
    line_number: int = Field(
        ...,
        ge=0,
        description="1-based line number of the vulnerable code. 0 means 'file-wide / not line-specific'.",
    )
    explanation: str = Field(
        ...,
        min_length=20,
        description=(
            "A 2-3 sentence reasoning chain explaining WHY this specific code "
            "pattern is risky, referencing the retrieved CWE context. Not a label."
        ),
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model's self-reported confidence in this finding, 0..1.",
    )

    @field_validator("cwe_id")
    @classmethod
    def _normalize_cwe(cls, v: str) -> str:
        """
        Normalize to canonical 'CWE-<digits>' form. LLMs frequently emit
        'cwe 89', 'CWE89', or 'CWE-89:'. We normalize rather than reject so a
        cosmetically-off-but-correct finding isn't discarded — but we still
        enforce the numeric core so garbage ('CWE-unknown') is rejected.
        """
        raw = v.strip().upper().replace(" ", "").replace(":", "")
        if raw.startswith("CWE-"):
            digits = raw[4:]
        elif raw.startswith("CWE"):
            digits = raw[3:]
        else:
            digits = raw
        if not digits.isdigit():
            raise ValueError(f"Unparseable CWE id: {v!r}")
        return f"CWE-{int(digits)}"


class ScanResult(BaseModel):
    """
    Full Scanner Agent output. `model_used` is threaded through so the
    observability layer can turn the routing decision into a span attribute
    (feature #4) without re-deriving it.
    """
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    # `model_` is a protected namespace in Pydantic v2, which warns on every
    # import. The field name is part of the public API surface (the frontend
    # and the audit log both read it), so the namespace is opted out of rather
    # than renaming the field.
    model_config = ConfigDict(protected_namespaces=())

    model_used: str = Field(..., description="Model string the scanner actually invoked.")
    retrieved_cwe_ids: list[str] = Field(
        default_factory=list,
        description="CWE ids injected into context via RAG, for auditability.",
    )
    tokens_used: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Total tokens (prompt + completion) the provider reported for this "
            "agent's call. None means nothing counted them — a streaming call, "
            "an SDK that omitted `usage`, or a monkeypatched client. None and 0 "
            "are different facts and must stay distinguishable: 0 would render "
            "as 'this call consumed no tokens', which is never true of a call "
            "that happened (the measured-numbers rule). Set by the agent from the provider "
            "response, never by the model's own JSON — see "
            "ai_agent._strip_measured_fields."
        ),
    )
    cost_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost of this agent's call, computed from the provider-reported "
            "prompt/completion split at the call site — the only place that split "
            "is known. None when nothing counted the tokens; see tokens_used for "
            "why None and 0.0 must stay distinguishable. Summed per request by "
            "backend/api/router.py:_compute_and_record_cost."
        ),
    )


# ---------------------------------------------------------------------------
# Fixer Agent output
# ---------------------------------------------------------------------------

class FixResult(BaseModel):
    """
    Fixer Agent output. We keep the diff summary as a discrete field (rather
    than making the frontend diff two blobs) because the LLM has the richest
    context on *what it changed and why* at generation time.
    """
    patched_code: str = Field(..., description="The full refactored, secured code.")
    diff_summary: str = Field(
        ...,
        min_length=10,
        description="Human-readable summary of the changes and the reasoning.",
    )
    addressed_cwe_ids: list[str] = Field(
        default_factory=list,
        description="CWE ids this patch claims to remediate. Cross-checked by the Validator.",
    )
    model_config = ConfigDict(protected_namespaces=())

    model_used: str = Field(...)
    tokens_used: Optional[int] = Field(
        default=None,
        ge=0,
        description="Provider-reported total tokens for this call. See ScanResult.tokens_used.",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="USD cost of this call. See ScanResult.cost_usd.",
    )


# ---------------------------------------------------------------------------
# Validator / Critic Agent output
# ---------------------------------------------------------------------------

class ValidationResult(BaseModel):
    """
    Validator/Critic output. `eval_score` drives the self-healing loop:
    below the threshold (85) we re-invoke the Fixer with `feedback` appended.

    We separate `verdict` from `eval_score` on purpose: a fix can numerically
    score 88 (pass threshold) yet the critic may still flag a residual issue,
    or score 84 yet be verdict=fail for a hard security reason. Keeping both
    lets downstream logic use whichever signal is appropriate.
    """
    eval_score: int = Field(..., ge=0, le=100, description="Overall quality/security score.")
    verdict: Verdict
    reasoning: str = Field(..., min_length=20, description="Why the validator reached this verdict.")
    unresolved_cwe_ids: list[str] = Field(
        default_factory=list,
        description="CWE ids from the original scan the validator believes are NOT fixed.",
    )
    feedback: str = Field(
        default="",
        description="Actionable feedback for the Fixer on the next reflection iteration.",
    )
    tokens_used: Optional[int] = Field(
        default=None,
        ge=0,
        description="Provider-reported total tokens for this call. See ScanResult.tokens_used.",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="USD cost of this call. See ScanResult.cost_usd.",
    )


# ---------------------------------------------------------------------------
# Reflection loop bookkeeping
# ---------------------------------------------------------------------------

class ReflectionAttempt(BaseModel):
    """
    One iteration of the self-healing loop. We persist EVERY attempt (feature
    #5) — not just the winner — so the observability/frontend layer can render
    the agent 'thinking out loud' and so we can debug loops that never
    converge.
    """
    attempt_number: int = Field(..., ge=1)
    fix: FixResult
    validation: ValidationResult


# ---------------------------------------------------------------------------
# SELF-OBSERVATION — telemetry-driven adaptation summary
# ---------------------------------------------------------------------------

class SelfObservationSummary(BaseModel):
    """
    SELF-OBSERVATION: summary of which telemetry-driven adaptations fired
    for this scan (via SigNoz MCP) — the bridge between "the agent reads its
    own telemetry" and "a human/judge can see that it did, without digging
    through SigNoz."

    Defined here (not in ai_agent.py) so PipelineResult can reference it
    directly without a string forward-reference.
    """
    routing_override: Optional[str] = Field(
        default=None,
        description="Reason the Fixer's model was overridden from severity-based default, if any (e.g. 'cost_budget_exceeded').",
    )
    context_k_adjusted: bool = Field(
        default=False,
        description="Whether RAG retrieval depth (k) was elevated based on historical CWE failure patterns.",
    )
    conservation_mode_active: bool = Field(
        default=False,
        description="Whether CostGuardian's global conservation mode was active during this scan.",
    )


class PipelineResult(BaseModel):
    """
    The top-level object returned by the orchestrator. This is the single
    contract the frontend/observability engineers integrate against.
    """
    original_code: str
    scan: ScanResult
    # Optional because a scan that reports no vulnerabilities does not run the
    # Fixer or the Validator — there is nothing to fix and nothing to review.
    # `final_validation` is None in that case rather than carrying a synthesised
    # eval_score: no model graded this code, so no number may imply one did
    # (the no-fabrication rule). backend/api/router.py already guards every read of these with
    # getattr(..., None), so Optional matches how they are actually consumed.
    final_fix: Optional[FixResult] = None
    final_validation: Optional[ValidationResult] = None
    reflection_history: list[ReflectionAttempt] = Field(default_factory=list)
    converged: bool = Field(
        ...,
        description="True if a fix passed the eval threshold within the retry budget.",
    )
    routing_decisions: dict[str, str] = Field(
        default_factory=dict,
        description="agent_name -> model_used, so routing is fully auditable.",
    )
    self_observation: Optional[SelfObservationSummary] = Field(
        default=None,
        description="Which telemetry-driven adaptations (SigNoz MCP) fired for this scan, if any.",
    )


# ---------------------------------------------------------------------------
# Benchmark harness output
# ---------------------------------------------------------------------------

class BenchmarkReport(BaseModel):
    """
    Final, frontend-displayable benchmark output (feature #8).

    We report precision/recall/accuracy AND false_positive_rate because for a
    *security* tool, false positives are the metric that kills adoption:
    developers stop trusting a scanner that cries wolf. A reviewer will
    ask "what's your FP rate?" — so we surface it as a first-class field.
    """
    total_snippets: int
    # the measured-numbers rule: a figure must not conflate two different facts. A snippet whose
    # scan RAISED is not the same as a snippet where the Scanner correctly found
    # nothing, but both used to land in false_negatives identically — so an API
    # outage published itself as poor detection quality with no way to tell.
    # `errored_snippets` makes a degraded run visibly degraded.
    errored_snippets: int = 0
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float = Field(..., ge=0.0, le=1.0)
    recall: float = Field(..., ge=0.0, le=1.0)
    accuracy: float = Field(..., ge=0.0, le=1.0)
    false_positive_rate: float = Field(..., ge=0.0, le=1.0)
    mean_confidence_on_hits: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Mean confidence on correct findings — a calibration signal.",
    )
    per_snippet: list[dict] = Field(
        default_factory=list,
        description="Per-snippet breakdown for drill-down in the UI.",
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AgentExecutionError(Exception):
    """
    Raised when an agent fails irrecoverably (API error, unparseable output
    after retries, schema violation). The circuit-breaker module upstream
    catches this to trigger fallback. We attach `agent` and `cause` so the
    breaker can make per-agent decisions (e.g. only trip on Fixer failures).
    """

    def __init__(self, agent: str, message: str, cause: Optional[Exception] = None):
        self.agent = agent
        self.cause = cause
        super().__init__(f"[{agent}] {message}")


# ---------------------------------------------------------------------------
# API-level request schema
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    """
    Incoming request body for POST /scan. Added at integration time — the
    AI/agent module works with raw `code: str`, but the API layer needs a
    validated request envelope (e.g. to reject empty/oversized payloads).
    """
    code: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="Raw source code to scan for vulnerabilities.",
    )
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description=(
            "Source language. One of the ids in backend.core.languages.LANGUAGES. "
            "Unknown values normalise to the default rather than rejecting the "
            "scan — the pipeline is prompt-driven and degrades sensibly."
        ),
    )

    @field_validator("language")
    @classmethod
    def _normalise_language(cls, v: str) -> str:
        """
        Lower-case and fall back to the default for anything unrecognised.

        Normalising here rather than rejecting keeps the endpoint tolerant of
        older clients (which sent no language at all) while guaranteeing that
        everything downstream — the cache key, the span attribute and the three
        agent prompts — sees an id that exists in the registry.
        """
        return resolve(v).id