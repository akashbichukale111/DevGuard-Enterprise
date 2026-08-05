"use client";

import { motion } from "framer-motion";
import { useMemo } from "react";

/**
 * TraceWaterfall — a compact, dependency-light horizontal span waterfall.
 *
 * Why hand-rolled SVG/flex instead of a charting lib:
 * - This widget's whole job is to be the *bridge* between the claim
 *   "we have distributed tracing" and the proof "here it is, inline, before
 *   you even click through to SigNoz." A 200KB charting dependency would
 *   contradict the "production-lean" story we're selling.
 * - Bars are positioned by real offset + duration (proportional), so the shape
 *   is truthful: a long fix_agent bar or a visible reflection_retry bar tells
 *   the self-correction story visually.
 *
 * Color scheme reasoning:
 * - We map bar color to *semantic role*, not to prettiness:
 *     scan   -> cyan   (perception / input)
 *     fix    -> violet (generation)
 *     retry  -> amber  (self-correction — deliberately the "warm" alert hue so
 *                       the eye lands on the reflection loop, our wow moment)
 *     validate -> emerald (verification / pass)
 *     cost   -> slate  (bookkeeping, intentionally muted)
 *   This makes the retry span pop without a label having to say "look here."
 */

export interface TraceSpan {
  /** Span name, e.g. "fix_agent" or "reflection_retry_1" */
  name: string;
  /** Start offset from trace root, in ms */
  startMs: number;
  /** Span duration, in ms */
  durationMs: number;
}

interface TraceWaterfallProps {
  spans: TraceSpan[];
  /** Total trace duration; if omitted, computed from spans */
  totalMs?: number;
}

type Role = "scan" | "fix" | "retry" | "validate" | "cost" | "default";

function roleFor(name: string): Role {
  const n = name.toLowerCase();
  if (n.includes("retry") || n.includes("reflect")) return "retry";
  if (n.includes("scan")) return "scan";
  if (n.includes("valid")) return "validate";
  if (n.includes("fix")) return "fix";
  if (n.includes("cost")) return "cost";
  return "default";
}

const ROLE_STYLES: Record<Role, { bar: string; dot: string }> = {
  scan: { bar: "bg-cyan-400/80", dot: "bg-cyan-400" },
  fix: { bar: "bg-violet-400/80", dot: "bg-violet-400" },
  retry: { bar: "bg-amber-400/90", dot: "bg-amber-400" },
  validate: { bar: "bg-emerald-400/80", dot: "bg-emerald-400" },
  cost: { bar: "bg-slate-400/70", dot: "bg-slate-400" },
  default: { bar: "bg-white/40", dot: "bg-white/60" },
};

export default function TraceWaterfall({ spans, totalMs }: TraceWaterfallProps) {
  const total = useMemo(() => {
    if (totalMs && totalMs > 0) return totalMs;
    const end = spans.reduce((m, s) => Math.max(m, s.startMs + s.durationMs), 0);
    return end || 1; // guard against divide-by-zero
  }, [spans, totalMs]);

  return (
    <div className="rounded-2xl border border-white/10 bg-white/5 p-4 backdrop-blur-xl">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-white/50">
          Distributed Trace · {spans.length} spans
        </span>
        <span className="font-mono text-[11px] text-white/40">
          {total.toFixed(0)} ms total
        </span>
      </div>

      <div className="space-y-2">
        {spans.map((span, i) => {
          const role = roleFor(span.name);
          const style = ROLE_STYLES[role];
          const leftPct = (span.startMs / total) * 100;
          const widthPct = Math.max((span.durationMs / total) * 100, 1.5); // floor so tiny spans stay visible

          return (
            <div key={`${span.name}-${i}`} className="group flex items-center gap-3">
              {/* Left rail: dot + span name */}
              <div className="flex w-36 shrink-0 items-center gap-2">
                <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
                <span className="truncate font-mono text-[11px] text-white/70">
                  {span.name}
                </span>
              </div>

              {/* Track */}
              <div className="relative h-4 flex-1 rounded bg-black/30">
                <motion.div
                  initial={{ scaleX: 0 }}
                  animate={{ scaleX: 1 }}
                  transition={{ duration: 0.5, delay: 0.05 * i, ease: "easeOut" }}
                  style={{
                    left: `${leftPct}%`,
                    width: `${widthPct}%`,
                    transformOrigin: "left",
                  }}
                  className={`absolute top-0.5 h-3 rounded ${style.bar} ${
                    role === "retry" ? "ring-1 ring-amber-300/50" : ""
                  }`}
                />
              </div>

              {/* Right rail: duration */}
              <span className="w-16 shrink-0 text-right font-mono text-[11px] tabular-nums text-white/50">
                {span.durationMs.toFixed(0)}ms
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
