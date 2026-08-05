"""
Role: "Every agent's allowlist is enforced in code and asserted in tests, so
'least privilege' is verifiable, not claimed."

This file is the "asserted in tests" half. The two assertions that carry real
security weight are:

  * Diagnostician holds zero tools — the agent that reads attacker-controllable
    catalog text cannot act on it, so the untrusted-content boundary's "no tool call may be selected on
    the basis of catalog free-text" is true structurally rather than by prompt
    discipline.
  * Only Scribe holds any mutation tool.

Both would still "work" if broken, which is exactly why they need a test: a
widened allowlist produces no error, no failing feature, and no visible symptom
until someone audits it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import timezone

import pytest

from backend.v2 import datahub_client as dhc
from backend.v2.handoff import (
    AGENT_TOOL_ALLOWLISTS, AgentDecision, AgentHandoff, MUTATION_TOOLS,
    ToolAllowlist, ToolNotAllowedError, now,
)


class TestZeroToolAgents:
    @pytest.mark.parametrize("agent", ["diagnostician", "sentinel", "watcher",
                                       "referee", "surgeon", "auditor"])
    def test_holds_no_tools(self, agent):
        assert AGENT_TOOL_ALLOWLISTS[agent] == frozenset()

    def test_diagnostician_refuses_every_tool(self):
        allowlist = ToolAllowlist("diagnostician")
        for tool in ("search", "get_lineage", "add_tags", "save_document",
                     "get_entities", "get_dataset_queries"):
            with pytest.raises(ToolNotAllowedError):
                allowlist.check(tool)

    def test_diagnostician_error_names_the_condition(self):
        """The message has to explain WHY, or the next reader just widens it."""
        with pytest.raises(ToolNotAllowedError, match="holds no tools"):
            ToolAllowlist("diagnostician").check("search")


class TestMutationIsolation:
    def test_only_scribe_holds_mutation_tools(self):
        offenders = {
            agent: sorted(tools & MUTATION_TOOLS)
            for agent, tools in AGENT_TOOL_ALLOWLISTS.items()
            if agent != "scribe" and (tools & MUTATION_TOOLS)
        }
        assert offenders == {}, f"non-Scribe agents hold mutation tools: {offenders}"

    def test_scribe_holds_only_the_five_write_back_artifacts(self):
        assert AGENT_TOOL_ALLOWLISTS["scribe"] <= MUTATION_TOOLS

    def test_read_agents_are_strictly_read_only(self):
        for agent in ("archivist", "cartographer", "pathfinder", "magistrate"):
            assert not (AGENT_TOOL_ALLOWLISTS[agent] & MUTATION_TOOLS)


class TestContractTables:
    """The table, transcribed. If the contract and the code drift, fail here."""

    @pytest.mark.parametrize("agent,expected", [
        ("archivist", {"search_documents", "grep_documents"}),
        ("cartographer", {"search", "get_entities", "list_schema_fields"}),
        ("pathfinder", {"get_lineage", "get_lineage_paths_between",
                        "get_dataset_queries"}),
        ("magistrate", {"get_entities"}),
    ])
    def test_matches_contract(self, agent, expected):
        assert AGENT_TOOL_ALLOWLISTS[agent] == frozenset(expected)

    def test_pathfinder_allowlist_is_exactly_step_6(self):
        """step 6 names three tools. Not two, not four."""
        assert len(AGENT_TOOL_ALLOWLISTS["pathfinder"]) == 3


class TestFailClosed:
    def test_unknown_agent_raises_rather_than_defaulting(self):
        with pytest.raises(KeyError, match="unknown agent"):
            ToolAllowlist("cartogropher")  # typo

    def test_membership_operator(self):
        allowlist = ToolAllowlist("pathfinder")
        assert "get_lineage" in allowlist
        assert "add_tags" not in allowlist


class TestEnforcementIsOnTheCallPath:
    """The check must run before any I/O, or it enforces nothing."""

    def test_call_checks_allowlist_first(self):
        """Parsed, not string-matched — a comment must not be able to satisfy this."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(dhc.DataHubMCPClient.call)))
        fn = tree.body[0]
        statements = [s for s in fn.body
                      if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        first = statements[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), (
            f"first statement of call() should be the allowlist check, got {ast.dump(first)[:120]}")
        callee = first.value.func
        assert isinstance(callee, ast.Attribute) and callee.attr == "check"
        assert isinstance(callee.value, ast.Call)
        assert getattr(callee.value.func, "id", None) == "ToolAllowlist"

    def test_overreach_raises_without_a_running_server(self):
        """No process is started, so this can only be the pre-flight check."""
        client = dhc.DataHubMCPClient(token="unused")
        with pytest.raises(ToolNotAllowedError):
            client.call("pathfinder", "add_tags", {})
        assert client._proc is None, "a rejected call must not have started the server"

    def test_telemetry_is_disabled_unconditionally(self):
        """Integration finding 17: without this, every call looks like a hang."""
        env = dhc.DataHubMCPClient(token="t")._env()
        assert env["DATAHUB_TELEMETRY_ENABLED"] == "false"

    def test_mutations_are_off_unless_requested(self):
        assert dhc.DataHubMCPClient(token="t")._env()["TOOLS_IS_MUTATION_ENABLED"] == "false"
        assert dhc.DataHubMCPClient(
            token="t", enable_mutations=True)._env()["TOOLS_IS_MUTATION_ENABLED"] == "true"


class TestHandoffEnvelope:
    def test_rationale_defaults_empty_and_is_display_only(self):
        h = AgentHandoff(from_agent="a", to_agent="b", incident_id="I",
                         decision=AgentDecision.OK, started_at=now(), ended_at=now())
        assert h.rationale == ""
        assert "display only" in AgentHandoff.model_fields["rationale"].description.lower()

    def test_evidence_ids_are_references_not_text(self):
        annotation = AgentHandoff.model_fields["evidence_ids"].annotation
        assert annotation == tuple[str, ...]

    def test_duration_is_measured_not_supplied(self):
        start = now()
        h = AgentHandoff(from_agent="a", to_agent="b", incident_id="I",
                         decision=AgentDecision.OK, started_at=start,
                         ended_at=start.replace(tzinfo=timezone.utc))
        assert h.duration_ms == 0.0
        assert "duration_ms" not in AgentHandoff.model_fields

    def test_handoff_is_frozen(self):
        h = AgentHandoff(from_agent="a", to_agent="b", incident_id="I",
                         decision=AgentDecision.OK, started_at=now(), ended_at=now())
        with pytest.raises(Exception):
            h.decision = AgentDecision.BLOCKED
