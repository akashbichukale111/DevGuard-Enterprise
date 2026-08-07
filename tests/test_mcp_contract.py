"""
Every DataHub tool this project names must exist in the published server.

The failure this prevents is specific and expensive. Tool names are strings
typed into allowlists and call sites. A typo, or a rename upstream, produces
code that passes every test here, deploys cleanly, and then fails on the one
path that needs a live DataHub — which is the path this project can least
afford to break and least easily test, since the capture environment has no
catalog.

Capability negotiation catches an absent tool at connect time, but only once a
connection exists, and it cannot distinguish "your server is older than this
client" from "this tool name was never real". Checking the pinned contract
catches the second case offline, in CI, with no network.

The contract in `backend/v2/mcp_contract.py` is a transcription of
mcp-server-datahub's published tool reference. It is deliberately an
*independent* source: deriving it from our own call sites would make every
assertion below a tautology.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.v2 import mcp_contract as contract
from backend.v2.handoff import (
    AGENT_TOOL_ALLOWLISTS,
    MUTATION_TOOLS,
)

REPO = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO / "backend" / "v2" / "agents"


def _allowlisted_tools() -> set[str]:
    tools: set[str] = set()
    for granted in AGENT_TOOL_ALLOWLISTS.values():
        tools |= set(granted)
    return tools


def _called_tool_names() -> dict[str, set[str]]:
    """Tool-name literals passed to `client.call(...)`, per agent module."""
    found: dict[str, set[str]] = {}
    for path in sorted(AGENTS_DIR.glob("*.py")):
        names = set(re.findall(r"\.call\(\s*self\.NAME\s*,\s*[\"']([a-z_]+)[\"']",
                               path.read_text()))
        if names:
            found[path.stem] = names
    return found


# --------------------------------------------------------------------------- #
# The contract itself
# --------------------------------------------------------------------------- #

def test_the_contract_records_where_it_came_from():
    """A pinned copy of someone else's API is worthless without provenance."""
    assert contract.CONTRACT_SOURCE.startswith("https://")
    assert contract.CONTRACT_RECORDED_UTC
    assert contract.CONTRACT_SERVER_VERSION


def test_the_tool_groups_do_not_overlap():
    """A tool in two groups means one of the gating flags is wrong."""
    groups = {
        "read": contract.READ_TOOLS,
        "mutation": contract.MUTATION_TOOLS,
        "user": contract.USER_TOOLS,
        "document": contract.DOCUMENT_TOOLS,
    }
    seen: dict[str, str] = {}
    for group, tools in groups.items():
        for tool in tools:
            assert tool not in seen, f"{tool} is in both {seen[tool]} and {group}"
            seen[tool] = group


def test_all_tools_is_the_union():
    assert contract.ALL_TOOLS == (
        contract.READ_TOOLS | contract.MUTATION_TOOLS
        | contract.USER_TOOLS | contract.DOCUMENT_TOOLS
    )


# --------------------------------------------------------------------------- #
# Our usage against it
# --------------------------------------------------------------------------- #

def test_every_allowlisted_tool_exists_in_the_published_server():
    unknown = sorted(_allowlisted_tools() - contract.ALL_TOOLS)
    assert not unknown, (
        f"allowlists grant tools mcp-server-datahub does not publish: {unknown}. "
        f"Either the name is wrong or the contract needs updating from "
        f"{contract.CONTRACT_SOURCE}"
    )


@pytest.mark.parametrize("module", sorted(_called_tool_names()))
def test_every_called_tool_exists_in_the_published_server(module):
    unknown = sorted(_called_tool_names()[module] - contract.ALL_TOOLS)
    assert not unknown, f"{module}.py calls tools the server does not publish: {unknown}"


@pytest.mark.parametrize("module", sorted(_called_tool_names()))
def test_every_called_tool_is_granted_to_the_agent_that_calls_it(module):
    """
    The allowlist is enforced at call time, so a mismatch is a guaranteed
    runtime failure rather than a style problem.
    """
    granted = AGENT_TOOL_ALLOWLISTS.get(module, frozenset())
    ungranted = sorted(_called_tool_names()[module] - set(granted))
    assert not ungranted, f"{module}.py calls tools it is not granted: {ungranted}"


def test_our_mutation_list_agrees_with_the_servers():
    """
    `handoff.MUTATION_TOOLS` decides which tools count as writes for the
    "only the Scribe may mutate" rule. If it drifts below the server's own
    mutation set, a real write tool stops being treated as one.
    """
    missing = sorted(contract.MUTATION_TOOLS - set(MUTATION_TOOLS))
    assert not missing, (
        f"the server classes these as mutations and we do not: {missing} — "
        "an unlisted write tool bypasses the single-writer rule"
    )


def test_save_document_counts_as_a_write():
    """
    The server groups it under documents rather than mutations, but it creates
    a catalog entity. Classing it as a read because of where it sits in a
    README would be a governance hole.
    """
    assert "save_document" in contract.WRITING_TOOLS
    assert "save_document" in MUTATION_TOOLS


# --------------------------------------------------------------------------- #
# The single-writer rule, checked against the server's definition of "write"
# --------------------------------------------------------------------------- #

def test_only_the_scribe_holds_any_writing_tool():
    offenders = {
        agent: sorted(set(granted) & contract.WRITING_TOOLS)
        for agent, granted in AGENT_TOOL_ALLOWLISTS.items()
        if agent != "scribe" and set(granted) & contract.WRITING_TOOLS
    }
    assert not offenders, f"non-Scribe agents hold write tools: {offenders}"


def test_the_scribe_holds_only_tools_that_actually_write():
    """
    The converse. A read tool in the writer's allowlist widens the blast radius
    of the one agent whose compromise matters most.
    """
    stray = sorted(set(AGENT_TOOL_ALLOWLISTS["scribe"]) - contract.WRITING_TOOLS)
    assert not stray, f"Scribe holds non-write tools: {stray}"


def test_the_reading_agents_are_granted_only_read_or_document_reads():
    readable = contract.READ_TOOLS | {"search_documents", "grep_documents"}
    for agent in ("archivist", "cartographer", "pathfinder", "magistrate"):
        granted = set(AGENT_TOOL_ALLOWLISTS[agent])
        assert granted <= readable, (
            f"{agent} is granted something outside the read surface: "
            f"{sorted(granted - readable)}"
        )


def test_the_zero_tool_agents_really_hold_zero():
    """
    The Diagnostician's refusal guarantee is structural: it cannot fetch
    evidence because it holds nothing to fetch with.
    """
    for agent in ("watcher", "sentinel", "diagnostician", "surgeon", "referee", "auditor"):
        assert AGENT_TOOL_ALLOWLISTS[agent] == frozenset(), (
            f"{agent} was granted tools; its guarantee is that it has none"
        )


# --------------------------------------------------------------------------- #
# Server configuration
# --------------------------------------------------------------------------- #

def test_the_mutation_flag_is_documented():
    """
    Without TOOLS_IS_MUTATION_ENABLED the server hides every write tool, and a
    run that negotiates them all away looks exactly like a successful dry run.
    That trap belongs in the deployment docs, not in someone's memory.
    """
    assert contract.REQUIRED_SERVER_FLAGS["TOOLS_IS_MUTATION_ENABLED"] == "true"
    docs = (REPO / "DEPLOYMENT.md").read_text() + (REPO / "SECURITY.md").read_text()
    assert "TOOLS_IS_MUTATION_ENABLED" in docs, (
        "the flag that silently disables every write is not documented"
    )
