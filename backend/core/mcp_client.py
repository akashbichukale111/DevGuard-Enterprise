"""
backend/core/mcp_client.py

Client for talking to the local SigNoz MCP server so DevGuard's own agents can
query their own observability data as a *decision input* — not just so a human
can look at a dashboard. e.g. the Fixer/Router can ask "has this CWE historically
caused retries?" or "is our recent error rate elevated?" before choosing a model
tier or a retry budget.

STATUS: UNVERIFIED ADAPTER — NOT A WORKING SIGNOZ MCP INTEGRATION.
================================================================
This is the honest resolution of the question the initial audit raised (see
docs/MCP_DECISION.md). Stated plainly so that nobody
has to read the code to find it out:

  * This client has NEVER completed a round trip against a real SigNoz MCP
    server. Not once, from any commit in this repository's history.
  * The transport below is an ASSUMPTION. It POSTs `{"tool", "arguments"}` to
    an HTTP path. Standard MCP is JSON-RPC 2.0, so this is probably wrong.
  * The tool names (`query_metrics`, `query_traces`, `query_spans`) and every
    response field the `_parse_*` methods read are likewise unconfirmed.

What is therefore TRUE about DevGuard's self-observation today: agents consume
their own telemetry through an in-process shadow (`local_telemetry.py`), behind
a stable interface with an MCP adapter ready to be wired. That sentence is
still interesting and it does not overclaim.

the telemetry-query question offers two branches: prove a real round trip and keep the
differentiator claim, or state precisely what happens. The second branch was
taken because the first is impossible in the current environment — every
container registry and SigNoz Cloud itself are blocked by egress policy, so no
SigNoz instance can be reached to verify against. Evidence for that blocker is
in docs/MCP_DECISION.md.

To finish the first branch later, only two things need to change: `_call_tool`'s
transport (swap the httpx POST for an MCP SDK ClientSession) and the three
`_parse_*` methods. Nothing downstream depends on either.

THE ONE RULE THIS FILE EXISTS TO ENFORCE:
    SigNoz/MCP being slow, down, or wrong must NEVER slow down, hang, or fail
    a user-facing scan. Every public method is fail-safe: on any error it logs
    a warning and returns an "unavailable" result. Callers should treat this
    client the way you'd treat an optional cache — nice when it's there, never
    load-bearing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from backend.core.local_telemetry import get_recent_cost_trend_local
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MCPNotConfiguredError(RuntimeError):
    """Raised when a query is attempted with no SigNoz MCP endpoint configured.

    Deliberately distinct from a transport error: "there is no server" and
    "the server did not answer" are different facts, and reporting them
    identically is what allowed an absent integration to be presented as a
    momentarily-degraded one.
    """

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# NOT CONFIGURED BY DEFAULT — this is deliberate. See the module docstring.
#
# This previously defaulted to "http://localhost:8000", which is DevGuard's own
# backend port under docker-compose. The "SigNoz MCP client" was therefore
# POSTing an invented request shape to DevGuard itself and getting a 404, then
# silently falling back — while the README claimed agents were querying SigNoz
# over MCP. Defaulting to empty makes "not configured" the honest, explicit
# state instead of a wrong guess that fails quietly.
SIGNOZ_MCP_URL = os.environ.get("SIGNOZ_MCP_URL", "").strip()

# Aggressive on purpose: this client backs decisions inside the hot path of a
# scan request. A slow MCP server should degrade to "no data," never to a slow
# scan.
DEFAULT_TIMEOUT_SECONDS = 2.5


# --------------------------------------------------------------------------- #
# Typed return models
# --------------------------------------------------------------------------- #

class CostTrend(BaseModel):
    """Recent LLM spend, derived from `llm.cost_usd` span attributes.

    `available=False` means SigNoz/MCP could not be reached or returned data
    we couldn't parse — callers must treat that as "no signal," not "zero
    cost." Never branch agent behavior on the numeric fields without checking
    `available` first.

    `source` says *where the numbers came from* and is a separate question
    from whether they are usable:

      - ``"signoz_mcp"``   retrieved from the SigNoz MCP server
      - ``"local_shadow"`` computed in-process by ``local_telemetry.py`` — real
                           for this process, and never retrieved telemetry.
                           Whether the numbers are measured or approximated is a
                           SEPARATE question, answered by ``exact`` below: the
                           shadow now records provider-reported cost when the SDK
                           supplies usage, and falls back to a chars/4 estimate
                           only when it does not. This docstring used to say
                           local_shadow was always "an estimate", which
                           understated it once exact costs were threaded in.
      - ``"unavailable"``  no data

    Callers that render provenance MUST branch on ``source``, not on
    ``available``. Conflating the two is what previously caused the in-process
    estimate to be surfaced to users as "live".
    """

    available: bool = True
    source: str = "signoz_mcp"
    window_minutes: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_request_usd: float = 0.0
    request_count: int = 0
    # Assumption (per task spec): spans carry `llm.served_by` identifying which
    # model tier handled the request, letting us break cost down by tier.
    cost_by_model: dict[str, float] = Field(default_factory=dict)
    exact: bool = Field(
        default=True,
        description=(
            "False when any sample in the window was approximated rather than "
            "read from provider-reported usage. Orthogonal to `source`: a "
            "local_shadow total can be exact, and a signoz_mcp total is exact by "
            "construction. Surfaced because this total gates "
            "COST_BUDGET_USD_PER_30MIN — a routing decision made on estimates "
            "should be able to say so."
        ),
    )
    error: Optional[str] = None


class PatternStats(BaseModel):
    """Historical Validator outcomes for scans involving a given CWE.

    Used so the Fixer/Router can ask, in effect, "does this class of bug tend
    to need a retry loop or a stronger model?" before it commits to a tier —
    turning the pipeline's own audit trail into a prior for its next decision.
    """

    available: bool = True
    cwe_id: str = ""
    sample_size: int = 0
    fail_rate: float = 0.0  # fraction of validator verdicts == "fail"
    retry_rate: float = 0.0  # fraction of scans needing >=1 reflection retry
    avg_attempts_to_pass: float = 0.0
    error: Optional[str] = None


class ErrorRateResult(BaseModel):
    """Wrapper so `get_error_rate` can signal unavailability without forcing
    callers to special-case a bare float (e.g. `-1.0`) as a sentinel."""

    available: bool = True
    window_minutes: int = 0
    error_rate: float = 0.0  # 0.0-1.0
    sample_size: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class SignozMCPClient:
    """Async, fail-safe client for the local SigNoz MCP server.

    This is deliberately a thin, defensive layer. Its job is not to be a rich
    SigNoz SDK — it's to let the pipeline ask a small number of specific
    questions about its own recent behavior, and to guarantee that asking
    those questions is never riskier than not asking them.
    """

    def __init__(
        self,
        base_url: str = SIGNOZ_MCP_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Client is created lazily / reused across calls rather than opened
        # per-request, so we're not paying connection setup cost on every
        # agent decision.
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the underlying connection pool. Call on app shutdown."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def is_configured(self) -> bool:
        """True only when a SigNoz MCP endpoint has actually been supplied.

        Callers use this to distinguish "we asked SigNoz and it was down" from
        "there is no SigNoz to ask" — two states the old code collapsed into
        one, which is how an unconfigured client ended up reported as live.
        """
        return bool(self._base_url)

    def capability_report(self) -> dict[str, Any]:
        """What this client can currently do, stated plainly.

        This is the honest stand-in for the capability negotiation the contract
        asks for. A real tool list can only be produced by connecting to a real
        MCP server and enumerating it; until that has happened, this reports
        the unverified status rather than a hard-coded list of tool names that
        nobody has confirmed exist.
        """
        return {
            "configured": self.is_configured(),
            "endpoint": self._base_url or None,
            "transport": "http-adapter (UNVERIFIED against a real MCP server)",
            "verified_against_real_server": False,
            "tool_list": None,
            "note": (
                "The SigNoz MCP wire protocol has never been confirmed from "
                "this codebase. Real MCP is JSON-RPC 2.0; this adapter assumes "
                "an HTTP envelope. Treat any success from it as unverified "
                "until a captured request/response round trip exists in "
                "a captured proof pack."
            ),
        }

    async def _call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Single choke point for talking to the MCP server.

        UNVERIFIED TRANSPORT. This assumes an HTTP endpoint accepting
        `{"tool": ..., "arguments": ...}` and returning `{"result": {...}}`.
        The real SigNoz MCP server has never been reached from this codebase,
        so that shape is an assumption, not a contract. Standard MCP is
        JSON-RPC 2.0 — if the live server speaks that, this method is the only
        place that changes: swap the httpx POST for an MCP ClientSession
        `call_tool(...)` and keep the `dict[str, Any]` return shape so the
        `_parse_*` methods below are untouched.

        When no endpoint is configured this raises immediately without
        performing any network I/O. That matters: the old default pointed at
        DevGuard's own port, so an unconfigured client issued a real HTTP
        request to itself, got a 404, and fell back — producing a fallback that
        looked like a transient outage rather than an absent integration.

        Raises on any failure — callers convert to an "unavailable" result.
        """
        if not self.is_configured():
            raise MCPNotConfiguredError(
                "SIGNOZ_MCP_URL is not set — no SigNoz MCP server to query. "
                "This is the expected state until the integration is verified."
            )
        client = await self._client()
        response = await client.post(
            "/mcp/tools/call",
            json={"tool": tool, "arguments": arguments},
        )
        response.raise_for_status()
        payload = response.json()
        # Be defensive about envelope shape too, since it's unverified.
        return payload.get("result", payload)

    # ----------------------------------------------------------------- #
    # Public, typed, fail-safe query methods
    # ----------------------------------------------------------------- #

    async def get_recent_cost_trend(self, minutes: int = 30) -> CostTrend:
        """What has this pipeline spent recently, and on which model tier?

        Self-observation use case: lets a router/budget-guard agent notice
        "we've spent 3x the usual amount in the last 30 minutes" and react
        (e.g. tighten model-tier routing) using its own telemetry as ground
        truth, instead of a human noticing a bill days later.

        Never raises. On any failure, returns `CostTrend(available=False)`.
        """
        try:
            raw = await self._call_tool(
                "query_metrics",
                {
                    # Assumption (per task spec): cost/model attributes exist
                    # on relevant LLM-call spans.
                    "metric": "llm.cost_usd",
                    "group_by": "llm.served_by",
                    "window_minutes": minutes,
                },
            )
            return self._parse_cost_trend(raw, minutes)
        except (
            MCPNotConfiguredError,
            httpx.HTTPError,
            httpx.TimeoutException,
            ValueError,
            KeyError,
        ) as exc:
            logger.info(
                "SignozMCPClient.get_recent_cost_trend: SigNoz/MCP unavailable "
                "(%s); falling back to local in-process cost telemetry. "
                "Result is labelled source='local_shadow', never 'live'.",
                exc,
            )
            local = get_recent_cost_trend_local(minutes)
            return CostTrend(
                available=True,
                # the provenance rule: this data is real for this process, but it did NOT come
                # from SigNoz — it is the in-process estimate from
                # local_telemetry.py. `available=True` alone previously caused
                # callers to badge it "live", which is how a local heuristic
                # ended up presented as retrieved telemetry. `source` is the
                # field callers must branch on to label provenance.
                source="local_shadow",
                window_minutes=minutes,
                total_cost_usd=local["total_cost_usd"],
                avg_cost_per_request_usd=local["avg_cost_per_request_usd"],
                request_count=local["request_count"],
                cost_by_model=local["cost_by_model"],
                # Provenance is `source`; accuracy is `exact`. Threading it
                # through means a caller that acts on this total can tell whether
                # it was measured or approximated.
                exact=local["exact"],
                error=f"MCP unreachable, using local fallback: {exc}",
            )

    async def get_error_rate(self, minutes: int = 30) -> float:
        """Recent pipeline failure rate (0.0-1.0), from span/trace error status.

        Self-observation use case: a spike in error rate is a cheap, fast
        signal an agent can check before making an expensive decision (e.g.
        "should I trust the Fast tier right now, or fail over to a stronger
        model given recent instability?").

        Returns 0.0 (not a raised exception) if SigNoz/MCP is unavailable —
        callers that need to distinguish "confirmed healthy" from "unknown"
        should use `get_error_rate_detailed` instead.
        """
        result = await self.get_error_rate_detailed(minutes=minutes)
        return result.error_rate if result.available else 0.0

    async def get_error_rate_detailed(self, minutes: int = 30) -> ErrorRateResult:
        """Same as `get_error_rate`, but preserves availability/sample size
        instead of collapsing an unknown state to 0.0. Prefer this version
        when the caller's logic actually branches on "do we trust this
        number," rather than just wanting a best-effort float.
        """
        try:
            raw = await self._call_tool(
                "query_traces",
                {
                    "filter": "status = error",
                    "window_minutes": minutes,
                    "aggregate": "rate",
                },
            )
            return self._parse_error_rate(raw, minutes)
        except (
            MCPNotConfiguredError,
            httpx.HTTPError,
            httpx.TimeoutException,
            ValueError,
            KeyError,
        ) as exc:
            logger.warning(
                "SignozMCPClient.get_error_rate_detailed: SigNoz/MCP "
                "unavailable or returned an unparseable response (%s). "
                "Returning unavailable result.",
                exc,
            )
            return ErrorRateResult(available=False, window_minutes=minutes, error=str(exc))

    async def get_cwe_failure_pattern(self, cwe_id: str) -> PatternStats:
        """How often has the Validator historically failed or needed retries
        when this CWE was involved?

        Self-observation use case: this is the client's most novel query —
        it turns the pipeline's own audit trail (already emitted as span
        attributes by the Validator/Fixer agents) into a prior the Router can
        consult *before* processing a new finding of the same CWE, e.g. to
        pre-emptively route straight to a stronger model tier or budget an
        extra reflection attempt for CWEs that are historically hard to fix
        cleanly on the first pass.

        Never raises. On any failure, returns
        `PatternStats(available=False, cwe_id=cwe_id)`.
        """
        try:
            raw = await self._call_tool(
                "query_spans",
                {
                    # Assumption (per task spec): Validator spans carry
                    # `cwe_id` and `verdict` attributes, and retry spans carry
                    # `reflection_attempt` counts.
                    "filter": f"cwe_id = '{cwe_id}'",
                    "group_by": "verdict",
                },
            )
            return self._parse_cwe_pattern(raw, cwe_id)
        except (
            MCPNotConfiguredError,
            httpx.HTTPError,
            httpx.TimeoutException,
            ValueError,
            KeyError,
        ) as exc:
            logger.warning(
                "SignozMCPClient.get_cwe_failure_pattern(%s): SigNoz/MCP "
                "unavailable or returned an unparseable response (%s). "
                "Returning unavailable result.",
                cwe_id,
                exc,
            )
            return PatternStats(available=False, cwe_id=cwe_id, error=str(exc))

    # ----------------------------------------------------------------- #
    # Response parsing
    # (Isolated from the public methods so the one place we need to touch
    # once the real MCP response shape is confirmed is small and obvious.)
    # ----------------------------------------------------------------- #

    @staticmethod
    def _parse_cost_trend(raw: dict[str, Any], minutes: int) -> CostTrend:
        # TODO: verify against real SigNoz MCP server response shape.
        series = raw.get("series", [])
        total = float(raw.get("total_cost_usd", 0.0))
        count = int(raw.get("request_count", 0))
        by_model = {
            str(point.get("group", "unknown")): float(point.get("value", 0.0))
            for point in series
        }
        avg = (total / count) if count > 0 else 0.0
        return CostTrend(
            available=True,
            source="signoz_mcp",
            window_minutes=minutes,
            total_cost_usd=total,
            avg_cost_per_request_usd=avg,
            request_count=count,
            cost_by_model=by_model,
        )

    @staticmethod
    def _parse_error_rate(raw: dict[str, Any], minutes: int) -> ErrorRateResult:
        # TODO: verify against real SigNoz MCP server response shape.
        rate = float(raw.get("rate", 0.0))
        sample_size = int(raw.get("sample_size", 0))
        return ErrorRateResult(
            available=True,
            window_minutes=minutes,
            error_rate=max(0.0, min(1.0, rate)),
            sample_size=sample_size,
        )

    @staticmethod
    def _parse_cwe_pattern(raw: dict[str, Any], cwe_id: str) -> PatternStats:
        # TODO: verify against real SigNoz MCP server response shape.
        groups = raw.get("groups", {})
        total = int(raw.get("sample_size", sum(int(v) for v in groups.values()) if groups else 0))
        fail_count = int(groups.get("fail", 0))
        retry_count = int(raw.get("retry_count", 0))
        avg_attempts = float(raw.get("avg_attempts_to_pass", 0.0))

        fail_rate = (fail_count / total) if total > 0 else 0.0
        retry_rate = (retry_count / total) if total > 0 else 0.0

        return PatternStats(
            available=True,
            cwe_id=cwe_id,
            sample_size=total,
            fail_rate=fail_rate,
            retry_rate=retry_rate,
            avg_attempts_to_pass=avg_attempts,
        )


# --------------------------------------------------------------------------- #
# Lazy singleton accessor
# --------------------------------------------------------------------------- #

_client_instance: Optional[SignozMCPClient] = None


def get_mcp_client() -> SignozMCPClient:
    """Return the process-wide `SignozMCPClient`, creating it on first use.

    Other modules (e.g. `self_observer.py`) should import and call this
    rather than constructing `SignozMCPClient` themselves, so the whole
    process shares one connection pool and one place to swap config/mocks
    for tests.
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = SignozMCPClient()
    return _client_instance
