# Catalog capability baseline

What the live DataHub server reported about itself before any agent ran. Every
version claim elsewhere in this repository is read back from these files rather
than taken from documentation.

| File | Contents |
|---|---|
| `datahub-config.json` | `GET /config` from the running instance — the authoritative version string |
| `mcp-tool-list.json` | The 18 tools the MCP server exposed, with descriptions |
| `mcp-tool-schemas.json` | Full input schemas for every tool |

**DataHub Core `v1.6.0`** (commit `059a36c0b035a6057de00114ccac0ea9003d6bc2`),
**MCP server `3.4.5`** speaking protocol `2024-11-05`.

The tool schemas matter beyond documentation: agent argument construction is
written against these captured schemas rather than guessed, which is why
`tests/test_agent_allowlists.py` can assert tool names and shapes without a live
server.

Note that `search_documents` and `grep_documents` are **absent from a clean
instance** — the server hides them when the catalog holds no documents. That is
the behaviour the Archivist's capability negotiation exists to handle.
