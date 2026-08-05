// frontend/lib/useScanSocket.ts
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── Wire protocol ──────────────────────────────────────────────────────────
// The backend emits one JSON object per line over the WebSocket. Each is a
// discriminated union on `type`. We keep this narrow and explicit so the UI
// never has to guess at shape (and so we never reach for `any`).

export type AgentName = "scanner" | "fixer" | "validator";
export type ModelTier = "fast" | "deep-reasoning";

export interface SpanEvent {
  type: "span";
  agent: AgentName;
  status: "started" | "progress" | "completed";
  /** Human-facing micro-status, e.g. "Found 3 vulnerabilities (CWE-89, CWE-79)". */
  message: string;
  /** Wall-clock duration of the span in ms, present on completion. */
  durationMs?: number;
}

export interface RoutingEvent {
  type: "routing";
  tier: ModelTier;
  model: string; // e.g. "devguard-sonnet" — shown small next to the tier badge
}

export interface CircuitBreakerEvent {
  type: "circuit_breaker";
  /** The provider/model that tripped, for the tasteful inline notice. */
  primary: string;
  fallback: string;
}

export interface DoneEvent {
  type: "done";
  scan_id: string;
  score: number; // final validator score 0-100
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export type ScanEvent =
  | SpanEvent
  | RoutingEvent
  | CircuitBreakerEvent
  | DoneEvent
  | ErrorEvent;

// ── Hook state ─────────────────────────────────────────────────────────────

export type ScanPhase = "idle" | "scanning" | "success" | "error";

/** A rendered status line — we keep an id so React can key + animate each in. */
export interface StatusLine {
  id: string;
  agent: AgentName | "system";
  message: string;
  durationMs?: number;
  ts: number;
}

export interface UseScanSocketState {
  phase: ScanPhase;
  lines: StatusLine[];
  routing: RoutingEvent | null;
  circuitBreaker: CircuitBreakerEvent | null;
  finalScanId: string | null;
  finalScore: number | null;
  error: string | null;
}

const WS_BASE =
  process.env.NEXT_PUBLIC_WS_BASE ?? "ws://localhost:8000";

const INITIAL_STATE: UseScanSocketState = {
  phase: "idle",
  lines: [],
  routing: null,
  circuitBreaker: null,
  finalScanId: null,
  finalScore: null,
  error: null,
};

/**
 * Subscribes to /ws/scan/{scanId} and reduces the event stream into UI state.
 *
 * Usage:
 *   const scan = useScanSocket();
 *   // after POST /scan resolves:
 *   scan.connect(scanId, traceId);
 *
 * The hook is intentionally imperative (connect/reset) rather than
 * auto-connecting on a prop, because the scan id only exists AFTER the POST
 * resolves — we don't want a render-driven effect racing the network call.
 */
export function useScanSocket() {
  const [state, setState] = useState<UseScanSocketState>(INITIAL_STATE);
  const socketRef = useRef<WebSocket | null>(null);
  const idCounter = useRef(0);

  const pushLine = useCallback(
    (agent: StatusLine["agent"], message: string, durationMs?: number) => {
      idCounter.current += 1;
      const line: StatusLine = {
        id: `line-${idCounter.current}`,
        agent,
        message,
        durationMs,
        ts: Date.now(),
      };
      setState((s) => ({ ...s, lines: [...s.lines, line] }));
    },
    []
  );

  const handleEvent = useCallback(
    (evt: ScanEvent) => {
      switch (evt.type) {
        case "span":
          pushLine(evt.agent, evt.message, evt.durationMs);
          break;
        case "routing":
          setState((s) => ({ ...s, routing: evt }));
          break;
        case "circuit_breaker":
          setState((s) => ({ ...s, circuitBreaker: evt }));
          pushLine(
            "system",
            `Primary model unavailable — automatically routed to fallback.`
          );
          break;
        case "done":
          setState((s) => ({
            ...s,
            phase: "success",
            finalScanId: evt.scan_id,
            finalScore: evt.score,
          }));
          break;
        case "error":
          setState((s) => ({ ...s, phase: "error", error: evt.message }));
          break;
      }
    },
    [pushLine]
  );

  const connect = useCallback(
    (scanId: string, traceId?: string) => {
      // Tear down any prior socket (defensive — a user could re-run).
      socketRef.current?.close();

      setState({ ...INITIAL_STATE, phase: "scanning" });

      // Thread the trace id into the query so the backend can correlate the
      // socket span with the /scan HTTP span under the same distributed trace.
      const q = traceId ? `?trace_id=${encodeURIComponent(traceId)}` : "";
      const ws = new WebSocket(`${WS_BASE}/ws/scan/${scanId}${q}`);
      socketRef.current = ws;

      ws.onmessage = (e: MessageEvent<string>) => {
        try {
          const parsed = JSON.parse(e.data) as ScanEvent;
          handleEvent(parsed);
        } catch {
          // Ignore malformed frames rather than crashing the demo.
        }
      };

      ws.onerror = () => {
        setState((s) =>
          // Only escalate to error if we haven't already succeeded — a socket
          // close after `done` is normal and must NOT flip us out of success.
          s.phase === "success"
            ? s
            : { ...s, phase: "error", error: "Lost connection to scan pipeline." }
        );
      };
    },
    [handleEvent]
  );

  const reset = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    setState(INITIAL_STATE);
  }, []);

  // Clean up the socket if the component unmounts mid-scan.
  useEffect(() => () => socketRef.current?.close(), []);

  return { ...state, connect, reset };
}
