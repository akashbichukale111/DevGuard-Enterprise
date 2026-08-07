"""
The published tool surface of `mcp-server-datahub`, pinned.

Why this file exists
--------------------
Every DataHub capability in this project is a *string*. `client.call(NAME,
"get_lineage_paths_between", args)` is a tool name typed into source code, and
nothing in the repository checked that the name is one the server actually
offers. A tool renamed upstream, or a typo in an allowlist, fails at runtime
against a live catalog — the one place this project has the least ability to
test, since the capture environment has no DataHub.

Capability negotiation at connect time catches an *absent* tool, but only after
a connection exists, and it cannot tell the difference between "this server is
older than you" and "you invented this name". This pins the contract so both
are caught offline, in CI, with no network and no catalog.

Provenance
----------
Taken from the tool reference in the mcp-server-datahub README:

    https://github.com/acryldata/mcp-server-datahub  (README.md, "Tools")

Recorded 2026-08-07 against `main`, matching the `mcp-server-datahub@0.6.0`
pin in this project's README badge. This is a transcription of a published
contract, not an inference from our own usage — the point is precisely that it
is an *independent* source to check our usage against, so deriving it from our
call sites would defeat it.

Keeping it current
------------------
When the server adds tools, add them here and the new capability becomes
available to allowlists. When it renames one, `tests/test_mcp_contract.py`
fails and names the call site that would have broken in production.
"""

from __future__ import annotations

#: Where this transcription came from, carried in-band so a reader does not
#: have to trust the module docstring alone.
CONTRACT_SOURCE = "https://github.com/acryldata/mcp-server-datahub#tools"
CONTRACT_RECORDED_UTC = "2026-08-07"
CONTRACT_SERVER_VERSION = "0.6.0"

#: Always available.
READ_TOOLS: frozenset[str] = frozenset({
    "search",
    "get_lineage",
    "get_dataset_queries",
    "get_entities",
    "list_schema_fields",
    "get_lineage_paths_between",
})

#: Gated behind `TOOLS_IS_MUTATION_ENABLED=true` on the server. Everything that
#: can change catalog state is here — the server's own definition of "mutation",
#: which is the authority this project's MUTATION_TOOLS must agree with.
MUTATION_TOOLS: frozenset[str] = frozenset({
    "add_tags", "remove_tags",
    "add_terms", "remove_terms",
    "add_owners", "remove_owners",
    "set_domains", "remove_domains",
    "update_description",
    "add_structured_properties", "remove_structured_properties",
})

#: Gated behind `TOOLS_IS_USER_ENABLED=true`.
USER_TOOLS: frozenset[str] = frozenset({"get_me"})

#: Document tools. `save_document` is additionally gated by
#: `SAVE_DOCUMENT_TOOL_ENABLED` (default true) and the whole group is hidden
#: when the catalog holds no documents — which is why the Archivist treats a
#: missing document tool as a capability gap rather than an error.
DOCUMENT_TOOLS: frozenset[str] = frozenset({
    "search_documents", "grep_documents", "save_document",
})

#: Every tool the server can expose, under any configuration.
ALL_TOOLS: frozenset[str] = (
    READ_TOOLS | MUTATION_TOOLS | USER_TOOLS | DOCUMENT_TOOLS
)

#: Tools that change state, for the purpose of "only the Scribe may write".
#: `save_document` is a write even though the server groups it under documents
#: rather than mutations — it creates a catalog entity, and grouping it with
#: reads because of where it sits in a README would be a governance hole.
WRITING_TOOLS: frozenset[str] = MUTATION_TOOLS | {"save_document"}

#: Server environment variables that must be set for the tools this project
#: uses to be present at all. Documented here because a run that silently
#: negotiates away every mutation tool looks identical to a successful dry run.
REQUIRED_SERVER_FLAGS: dict[str, str] = {
    "TOOLS_IS_MUTATION_ENABLED": "true",
}
