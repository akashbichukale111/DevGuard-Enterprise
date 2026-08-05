# Design decision: the SigNoz MCP path

**Status:** Decided · **Date:** 2026-07-29

**Decision: state precisely what happens, and withdraw the "agents query their
own SigNoz telemetry via MCP" claim until a real round trip exists.**

An overclaim a reviewer can disprove costs more than the feature was worth. This
record exists so that the reasoning is inspectable rather than inferred from a
gap in the documentation.

---

## The question

The self-observation layer was designed so that agents read their own telemetry —
recent spend, error rate, per-CWE failure history — and use it to change what
they do next. The intended source was a SigNoz MCP server.

Two options were available: wire the client to the real protocol and back the
claim with a captured round trip, or state exactly what the code does today.

---

## What the audit found

Three problems, confirmed by reading the code and reproduced at runtime.

1. **The transport was invented.** `_call_tool` POSTs
   `{"tool": ..., "arguments": ...}` to `/mcp/tools/call`. Standard MCP is
   JSON-RPC 2.0. The file carried its own TODO noting the shape had never been
   verified.

2. **The default endpoint pointed at DevGuard itself.** `SIGNOZ_MCP_URL`
   defaulted to `http://localhost:8000` — the backend's own port under Compose.
   Out of the box, the "SigNoz MCP client" issued a real HTTP request to
   DevGuard, received a 404, and fell back. It had never spoken to SigNoz.

3. **The fallback misreported its own provenance.** `get_recent_cost_trend`
   returned `available=True` on failure, and the caller read that as "live" — so
   an in-process estimate reached users labelled as retrieved telemetry. This is
   the most serious of the three, because the other two are missing features
   while this one is a false statement.

---

## Why the first option was not available

Not a matter of effort. Every path to a SigNoz instance was closed in the
environment where this work was done:

- **Every container registry was blocked by egress policy.** Docker Hub's CDN,
  GitHub Container Registry, Quay, Amazon ECR Public and `registry.k8s.io` all
  returned `403` to `CONNECT`. No SigNoz image could be pulled.
- **SigNoz Cloud was unreachable.** Both `ingest.us.signoz.cloud` and
  `signoz.io` failed to connect.

The Docker daemon itself was fine. The blocker was purely registry egress.

De-risking before building further is the reason this was resolved rather than
deferred: continuing to build on an unprovable capability compounds the problem.

---

## What changed

- `SIGNOZ_MCP_URL` defaults to **empty**, not to DevGuard's own port. "Not
  configured" is now an explicit state rather than a wrong guess that fails
  quietly.
- `MCPNotConfiguredError` is raised **before any network I/O** when no endpoint
  is set. "There is no server" and "the server did not answer" are different
  facts and are no longer reported identically.
- `SignozMCPClient.is_configured()` and `capability_report()` were added. The
  latter is the honest stand-in for capability negotiation: it reports
  `verified_against_real_server: false` and `tool_list: null` rather than a
  hard-coded list of tool names nobody has confirmed exist.
- `GET /telemetry-status` was added, so the claim is inspectable at runtime
  rather than taken on trust from documentation.
- The fallback reports `data_source: "local_shadow"`, never `live`.

---

## What may be claimed

> DevGuard's agents consume their own telemetry as a decision input — recent
> spend, error rate, and per-CWE failure history — through an in-process
> telemetry shadow, behind a stable typed interface with a SigNoz MCP adapter
> ready to be wired. The MCP path itself is not yet verified against a live
> server.

That is accurate, and the behaviour it describes is real: the router does change
model tier based on measured spend, and the cost figures it reads are
provider-reported where the SDK supplies them.

## What may not be claimed

- "Agents query their own SigNoz telemetry via MCP"
- "MCP-based self-observation"
- Anything implying a verified SigNoz MCP integration

---

## What would close this out

1. Reach a SigNoz instance — an unrestricted network, or an external host.
2. Start the SigNoz MCP server and **enumerate its tools**; commit the list.
3. Point `_call_tool` at the real transport — an MCP SDK `ClientSession`, not
   the current HTTP envelope.
4. Capture one real request/response round trip as a committed artifact.
5. Reinstate the claim, now backed by that artifact.

Only `_call_tool` and the three `_parse_*` methods need to change. Nothing
downstream depends on either, which is the part of the original design that held
up.

---

Related: [Architecture](../ARCHITECTURE.md) · [API](API.md) · [README limitations](../README.md#limitations)
