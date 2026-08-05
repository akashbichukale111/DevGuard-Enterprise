// frontend/lib/otel-frontend.ts
//
// W3C Trace Context propagation for the DevGuard AI frontend.
// Spec: https://www.w3.org/TR/trace-context/
//
// A `traceparent` header looks like:
//   00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
//   ^  ^                                ^                ^
//   |  |                                |                └─ flags (2 hex)  01 = sampled
//   |  |                                └─ parent span id (16 hex / 8 bytes)
//   |  └─ trace id (32 hex / 16 bytes)
//   └─ version (2 hex) — currently always "00"
//
// We generate a fresh trace id per top-level user action (a scan submission)
// so the entire pipeline — frontend → /scan → 3 agents → validator — rolls up
// under one trace in the backend's tracing UI.

const TRACE_VERSION = "00";
const SAMPLED_FLAG = "01"; // always sample demo traffic — we WANT every scan traced

/** Return `bytes` random bytes rendered as lowercase hex. */
function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  // crypto.getRandomValues is available in all modern browsers + Next.js edge.
  // We deliberately avoid Math.random() — trace ids must be high-entropy so
  // they don't collide across concurrent scans in the tracing backend.
  crypto.getRandomValues(buf);
  let out = "";
  for (let i = 0; i < buf.length; i++) {
    out += buf[i].toString(16).padStart(2, "0");
  }
  return out;
}

export interface TraceContext {
  traceId: string; // 32 hex chars
  spanId: string; // 16 hex chars
  traceparent: string; // full header value
}

/**
 * Generate a valid W3C traceparent header value.
 *
 * The returned object also exposes the raw trace/span ids so the caller can,
 * if desired, surface the trace id in the UI ("copy trace id") or thread it
 * into a WebSocket handshake for correlation.
 */
export function generateTraceparent(): TraceContext {
  const traceId = randomHex(16); // 16 bytes → 32 hex
  const spanId = randomHex(8); //  8 bytes → 16 hex
  const traceparent = `${TRACE_VERSION}-${traceId}-${spanId}-${SAMPLED_FLAG}`;
  return { traceId, spanId, traceparent };
}

export interface FetchWithTraceResult<T> {
  data: T;
  trace: TraceContext;
  response: Response;
}

/**
 * fetch() wrapper that mints a fresh trace context and injects the
 * `traceparent` header, then parses the JSON body as `T`.
 *
 * We return the trace context alongside the data so the caller can reuse the
 * same trace id when opening the status WebSocket (keeps the whole scan under
 * one distributed trace).
 *
 * Throws on non-2xx so callers can drive the UI error state from a single
 * try/catch rather than manually inspecting response.ok everywhere.
 */
export async function fetchWithTrace<T = unknown>(
  input: string,
  init: RequestInit = {},
  trace: TraceContext = generateTraceparent()
): Promise<FetchWithTraceResult<T>> {
  const headers = new Headers(init.headers);
  headers.set("traceparent", trace.traceparent);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(input, { ...init, headers });

  if (!response.ok) {
    // Surface the status so the UI can distinguish "server down" (5xx / network)
    // from "bad request" (4xx). The circuit-breaker UI keys off this.
    const text = await response.text().catch(() => "");
    throw new FetchTraceError(
      `Request to ${input} failed with ${response.status}`,
      response.status,
      text,
      trace
    );
  }

  const data = (await response.json()) as T;
  return { data, trace, response };
}

/** Typed error so the UI can branch on HTTP status / attach the trace id to logs. */
export class FetchTraceError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: string,
    public readonly trace: TraceContext
  ) {
    super(message);
    this.name = "FetchTraceError";
  }
}
