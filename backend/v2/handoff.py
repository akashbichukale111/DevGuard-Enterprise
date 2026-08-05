"""
handoff.py — the agent design AgentHandoff envelope, and the tool allowlist that makes
"least privilege" a property of the code rather than a claim in a README.

Role: "Every agent's allowlist is enforced in code and asserted in tests, so
'least privilege' is verifiable, not claimed." That sentence is the reason
`ToolAllowlist` lives here instead of in a config file nobody reads: the check
runs on the call path, so an agent that reaches for a tool it was not granted
raises before any network I/O happens.

The security properties the agent design says should fall out of this design, and where they
actually do:

  * Diagnostician has no tools     -> AGENT_TOOL_ALLOWLISTS["diagnostician"] is
                                      empty, and ToolAllowlist.check refuses
                                      everything.
  * Only Scribe can mutate         -> the mutation tools appear in exactly one
                                      allowlist, asserted in tests.
  * rationale is display-only      -> typed as such, and never fed back into a
                                      prompt by any agent in this package.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentDecision(str, Enum):
    """What an agent concluded. the handoff envelope calls this `decision`."""

    OK = "OK"
    """Produced its output; the next agent may proceed."""

    DEGRADED = "DEGRADED"
    """Produced partial output because a capability was unavailable.

    This is the value that makes capability negotiation honest. The pinned environment's trap says
    `search_documents` / `grep_documents` are hidden when the catalog has no
    documents; the Archivist must "degrade deliberately, never throw". DEGRADED
    is that deliberate degradation, and it is visible to everything downstream.
    """

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    """The evidence rule's refusal. The chain could not form, so the loop stops."""

    BLOCKED = "BLOCKED"
    """An external dependency the agent needs is unreachable."""


class ToolCallRecord(BaseModel):
    """One tool invocation, captured for the proof pack (the generated-numbers rule)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict = Field(default_factory=dict)
    ok: bool
    duration_ms: float
    raw_ref: Optional[str] = None
    error: Optional[str] = None


class AgentHandoff(BaseModel):
    """The typed envelope. The UI's handoff rail renders these.

    `evidence_ids` is deliberately a list of ids and not a list of Evidence
    objects. Role: "evidence_ids: [ ... ]  # never free text — always references".
    Passing ids forces the receiving agent to resolve them against the one
    canonical chain, so two agents can never disagree about what EV-…-004 says.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_agent: str
    to_agent: str
    incident_id: str
    evidence_ids: tuple[str, ...] = ()
    decision: AgentDecision
    rationale: str = Field(
        default="",
        description="Display only. Never an instruction, never fed to a model.",
    )
    started_at: datetime
    ended_at: datetime
    tokens: int = 0
    model: Optional[str] = None
    tool_calls: tuple[ToolCallRecord, ...] = ()

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() * 1000.0


class ToolNotAllowedError(PermissionError):
    """Raised when an agent reaches for a tool outside its allowlist."""

    def __init__(self, agent: str, tool: str, allowed: frozenset[str]) -> None:
        self.agent, self.tool, self.allowed = agent, tool, allowed
        super().__init__(
            f"agent {agent!r} may not call {tool!r}; "
            f"allowlist = {sorted(allowed) or '(none — this agent holds no tools)'}"
        )


#: The table, transcribed. Read tools only, except for Scribe.
#:
#: Two entries are load-bearing rather than descriptive:
#:   * "diagnostician" is empty ON PURPOSE. It is the agent that reads
#:     attacker-controllable catalog text, and it holds no tools so that text
#:     cannot cause a tool call (the untrusted-content boundary).
#:   * "scribe" is the only key containing any mutation tool.
AGENT_TOOL_ALLOWLISTS: dict[str, frozenset[str]] = {
    "watcher": frozenset(),  # deterministic; RuntimeEvidenceProvider only, not MCP
    "archivist": frozenset({"search_documents", "grep_documents"}),
    "cartographer": frozenset({"search", "get_entities", "list_schema_fields"}),
    "pathfinder": frozenset({"get_lineage", "get_lineage_paths_between",
                             "get_dataset_queries"}),
    "sentinel": frozenset(),  # scanner only; Role: "no DataHub tools"
    "diagnostician": frozenset(),  # Role: "LLM, zero tools"
    "surgeon": frozenset(),  # git/patch, not MCP
    "referee": frozenset(),  # test runner, not MCP
    "magistrate": frozenset({"get_entities"}),  # owners, read-only
    "scribe": frozenset({"add_tags", "update_description", "save_document",
                         "add_structured_properties", "add_owners"}),
    "auditor": frozenset(),  # filesystem
}

#: Everything that writes. Used by the tests that assert only Scribe can mutate.
MUTATION_TOOLS: frozenset[str] = frozenset({
    "add_tags", "remove_tags", "add_terms", "remove_terms", "add_owners",
    "remove_owners", "set_domains", "remove_domains", "update_description",
    "add_structured_properties", "remove_structured_properties",
    "set_lifecycle_stage", "save_document",
})


#: The mutation allowlist requires the mutation allowlist to enumerate "which tools, which entity
#: types, which domain". Tools are above; these are the other two axes.
#:
#: This duplicates the DataHub Access Policy created by
#: `scripts/setup_service_account.py`, on purpose. The policy is the real
#: control — it is enforced by the server and cannot be bypassed by a bug in
#: our code. This is the application-layer half of defence in depth, and it
#: fails *closed* and *early*: an out-of-scope write never leaves the process,
#: so it never depends on the server being configured correctly.
#:
#: That distinction stopped being theoretical against a live server: the DataHub quickstart
#: ships with `METADATA_SERVICE_AUTH_ENABLED=false`, under which Access Policies
#: are not enforced at all. Until that was found, the server-side control was
#: silently absent. This check would still have held.
MUTABLE_ENTITY_TYPES: frozenset[str] = frozenset({"dataset", "document"})

#: The exact assets DevGuard may write to. Same five datasets as the Access
#: Policy. Documents have no stable URN before creation, so they are scoped by
#: entity type only.
MUTATION_SCOPE_URNS: frozenset[str] = frozenset({
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.orders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_staging.stg_users,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_staging.stg_orders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.analytics_marts.user_order_features,PROD)",
})

#: Argument names that carry the entity a mutation targets. Read from the live
#: inputSchemas against the live server rather than guessed.
_TARGET_ARGUMENTS = ("entity_urns", "entity_urn", "urns", "urn", "related_assets")


class EntityNotInScopeError(PermissionError):
    """Raised when a mutation targets an asset outside the mutation allowlist allowlist."""

    def __init__(self, tool: str, urn: str) -> None:
        self.tool, self.urn = tool, urn
        super().__init__(
            f"{tool!r} may not mutate {urn!r}: it is outside DevGuard's declared "
            f"mutation scope (the mutation allowlist). Widening the scope is a config change and a "
            f"code review, not a runtime decision.")


def entity_type_of(urn: str) -> str:
    """`urn:li:<entityType>:(...)` -> `<entityType>`."""
    parts = urn.split(":", 3)
    return parts[2] if len(parts) > 2 else ""


def check_mutation_scope(tool: str, arguments: dict) -> None:
    """Fail closed if a mutation names an entity outside the allowlist.

    Only mutation tools are checked. Reads are deliberately unrestricted: the
    blast radius of reading a dataset DevGuard does not own is nil, and
    narrowing reads would break lineage traversal, which is the entire point of
    the Pathfinder.
    """
    if tool not in MUTATION_TOOLS:
        return
    for name in _TARGET_ARGUMENTS:
        value = arguments.get(name)
        if value is None:
            continue
        for urn in (value if isinstance(value, (list, tuple)) else [value]):
            if not isinstance(urn, str) or not urn.startswith("urn:li:"):
                continue
            if entity_type_of(urn) not in MUTABLE_ENTITY_TYPES:
                raise EntityNotInScopeError(tool, urn)
            if entity_type_of(urn) == "dataset" and urn not in MUTATION_SCOPE_URNS:
                raise EntityNotInScopeError(tool, urn)


class ToolAllowlist:
    """Enforcement, not documentation.

    Deliberately fails closed on an unknown agent name: a typo in an agent id
    should not silently grant a fresh, empty-but-permissive capability set.
    """

    def __init__(self, agent: str) -> None:
        if agent not in AGENT_TOOL_ALLOWLISTS:
            raise KeyError(
                f"unknown agent {agent!r}; add it to AGENT_TOOL_ALLOWLISTS with an "
                f"explicit allowlist rather than calling tools unregistered"
            )
        self.agent = agent
        self.allowed = AGENT_TOOL_ALLOWLISTS[agent]

    def check(self, tool: str) -> None:
        if tool not in self.allowed:
            raise ToolNotAllowedError(self.agent, tool, self.allowed)

    def __contains__(self, tool: object) -> bool:
        return tool in self.allowed


def now() -> datetime:
    return datetime.now(timezone.utc)
