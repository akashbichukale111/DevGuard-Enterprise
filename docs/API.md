# API reference

The DevGuard Enterprise backend is a FastAPI application. Interactive documentation is generated automatically and served alongside the API:

| | |
|---|---|
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| OpenAPI schema | `http://localhost:8000/openapi.json` |

Start it with:

```bash
python -m uvicorn backend.main:app --port 8000
```

The server starts and every endpoint below responds **without** an API key. Only `POST /scan` needs one, and it fails with a clear error rather than at import time.

---

## Conventions

**Tracing.** Every request is instrumented. Pass a W3C `traceparent` header to join an existing distributed trace; the header is propagated through the pipeline and appears on every child span.

**Honest nulls.** Any field that could not be measured is `null` and is accompanied by a reason field. The API never substitutes a plausible zero — the frontend renders `N/A` for these rather than a number.

**Provenance.** Responses that could be served from different sources carry a `data_source` field. It is never `live` unless the value genuinely came from a live dependency.

**CORS** is open by default (`allow_origins=["*"]`, credentials disabled). Restrict it before exposing the API beyond a trusted network.

---

## Scanning

### `POST /scan`

Runs the Scanner → Fixer → Validator reflection loop over a source snippet.

**Request**

```json
{
  "code": "query = \"SELECT * FROM users WHERE id = \" + user_id",
  "language": "python"
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `code` | string | yes | 1–50,000 characters |
| `language` | string | no | Defaults to `python` |

**Headers**

| Header | Purpose |
|---|---|
| `traceparent` | W3C trace context; joins the scan to an existing trace |

**Response** — the scan result, including the agent history for each reflection attempt, the converged fix, the severity classification, the hash-chained audit entry, and a `self_observation` block describing any telemetry-driven adaptation that fired during the request.

**Behaviour worth knowing**

- Scans classified `critical` or `high` do not finalise. They enter an approval gate and must be resolved through `/scan/{scan_id}/approve` or `/scan/{scan_id}/reject`.
- Clean code short-circuits: the Fixer and Validator do not run when the Scanner finds nothing.
- Results are cached. A repeated scan of identical code is served from cache and marked as such.
- Requires `GROQ_API_KEY`. Without it the endpoint returns an error explaining exactly that; the rest of the API is unaffected.

---

### `GET /scan/{scan_id}`

Retrieves a previously completed scan, including one awaiting approval.

Returns `404` if the ID is unknown or its state has been evicted. In-memory scan state is bounded — old entries are evicted rather than accumulated — so this is a short-lived handle, not durable storage. The audit log is the durable record.

---

### `POST /scan/{scan_id}/approve`

Approves a gated `critical` / `high` finding and finalises the scan. The decision is written to the audit trail.

### `POST /scan/{scan_id}/reject`

Rejects a gated finding.

| Parameter | In | Type | Required |
|---|---|---|---|
| `reason` | query | string | no |

The reason, when supplied, is recorded in the audit trail alongside the decision.

---

### `WS /ws/scan/{scan_id}`

WebSocket stream of span events for a scan, letting the UI show the reflection loop progressing rather than waiting on a single response.

Events are buffered per scan, so a client that connects slightly after the scan starts still receives the events it missed. The socket closes when the scan reaches a terminal state.

---

## Audit trail

### `GET /audit-log`

Returns the most recent audit entries.

| Parameter | In | Type | Default |
|---|---|---|---|
| `limit` | query | integer | `100` |

Each entry is hash-chained to its predecessor:

```json
{
  "scan_id": "acea993e-f88f-4f25-8bfb-188f382879c0",
  "timestamp": 1784280855.75,
  "code_hash": "ed7480ac…",
  "verdict": "vulnerable",
  "prev_hash": "0000…",
  "entry_hash": "d6f69d13…"
}
```

The read is paginated at the storage layer — it does not parse the whole log to return one page — and runs off the event loop.

### `GET /audit-log/verify`

Recomputes the chain and reports whether it is intact, naming the first entry at which verification failed if it is not.

This is the tamper-evidence check: modifying any historical entry invalidates every hash after it. Verification runs in a worker thread so a large log cannot block the event loop.

---

## Operations

### `GET /slo-status`

Latency and availability against the configured SLO, computed from recorded samples.

Responds successfully **even when the collector is unreachable**. Telemetry is fail-safe by design, and this endpoint is one of the places that property is regression-tested.

### `GET /telemetry-status`

Reports the state of the telemetry stack: whether an OTLP endpoint is configured, whether a SigNoz MCP URL is set, and which data source the self-observation layer is currently reading.

`data_source` is `local_shadow` whenever the MCP path is unavailable — which is its default state. It is never reported as `live` on an unverified path.

---

## Simulators

`POST /god-mode/simulate/{scenario}`

| Scenario | Path |
|---|---|
| Error burst | `/god-mode/simulate/error` |
| Cost spike | `/god-mode/simulate/cost-spike` |
| Memory leak | `/god-mode/simulate/memory-leak` |
| Hallucination | `/god-mode/simulate/hallucination` |
| Combined | `/god-mode/simulate/god-mode` |

These drive the operations panels for demonstration purposes. Each accepts an optional body and defaults to a synthetic scenario when called with an empty one.

**Every response is badged with its real provenance** — `LIVE`, `LOCAL`, `SIMULATED` or `PARTIAL` — and the UI renders that badge. Called with an empty body, which is what the UI currently sends, most responses are `SIMULATED`. That is a labelled property of the endpoint, not a hidden one.

---

## Errors

| Status | Meaning |
|---|---|
| `400` | Request body failed validation — empty, oversized, or malformed |
| `404` | Unknown or evicted `scan_id` |
| `409` | Approval or rejection attempted on a scan not awaiting a decision |
| `500` | Upstream failure; the circuit breaker records it and may open |
| `503` | Model provider unavailable and no fallback succeeded |

When the circuit breaker opens, a postmortem agent writes a short plain-English root cause into the audit trail at the moment of the transition.

---

## Configuration

Set in `.env` — see [`.env.example`](../.env.example) for the annotated list.

| Variable | Required | Effect when unset |
|---|---|---|
| `GROQ_API_KEY` | for `POST /scan` only | Server runs; `POST /scan` returns a clear error |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | Defaults to `http://localhost:4317`; telemetry no-ops if nothing is listening |
| `SIGNOZ_MCP_URL` | no | Self-observation uses the in-process shadow, reported as `local_shadow` |
| `AUDIT_LOG_PATH` | no | Defaults to `data/audit_log.jsonl`; created on first write |
| `COST_BUDGET_USD_PER_30MIN` | no | Cost guardian uses its built-in default |

Run `make doctor` to see which of these are set and what each missing one costs you.

---

Related: [Architecture](../ARCHITECTURE.md) · [Installation](INSTALLATION.md) · [Deployment](../DEPLOYMENT.md) · [Security](../SECURITY.md)
