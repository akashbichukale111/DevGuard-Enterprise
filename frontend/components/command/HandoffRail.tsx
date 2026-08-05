/**
 * HandoffRail.tsx — the Command Center: the spine of the screen.
 *
 * "One node per agent from the agent design, in order" — not one per handoff record. The
 * distinction matters: an agent that never ran is information, and a rail built
 * from records alone would silently omit it.
 *
 * Two states here exist because the data forced them:
 *   * `ran_no_record` — the Sentinel writes `patch-scan.json` but the Surgeon
 *     owns the edge into it, so the Sentinel emits no handoff of its own.
 *     Rendering it `idle` would claim a security control did not run when it
 *     did; its duration stays N/A rather than borrowing the Surgeon's.
 *   * `blocked` vs `refused` — the dry-run packs stop at the Diagnostician with
 *     BLOCKED (no reasoner configured) and its own rationale insists this is
 *     "NOT a refusal — the question was never asked". Only
 *     INSUFFICIENT_EVIDENCE is drawn as a refusal.
 */

"use client";

import React, { useState } from "react";
import { fmt, type RailNode, type ReplayBundle } from "@/lib/replay";
import { Badge, NA, Panel } from "./primitives";

const STATE_STYLE: Record<string, { ring: string; dot: string; label: string; tone: string }> = {
  done: {
    ring: "border-emerald-500/40 bg-emerald-500/[0.07]",
    dot: "bg-emerald-400",
    label: "done",
    tone: "text-emerald-300",
  },
  degraded: {
    ring: "border-amber-500/40 bg-amber-500/[0.07]",
    dot: "bg-amber-400",
    label: "degraded",
    tone: "text-amber-300",
  },
  blocked: {
    ring: "border-orange-500/40 bg-orange-500/[0.07]",
    dot: "bg-orange-400",
    label: "blocked",
    tone: "text-orange-300",
  },
  refused: {
    ring: "border-rose-500/50 bg-rose-500/[0.09]",
    dot: "bg-rose-400",
    label: "refused",
    tone: "text-rose-300",
  },
  ran_no_record: {
    ring: "border-sky-500/30 bg-sky-500/[0.05]",
    dot: "bg-sky-400/70",
    label: "ran",
    tone: "text-sky-300",
  },
  idle: {
    ring: "border-white/10 bg-white/[0.02]",
    dot: "bg-slate-700",
    label: "idle",
    tone: "text-slate-600",
  },
};

function NodeCard({
  node,
  selected,
  onSelect,
}: {
  node: RailNode;
  selected: boolean;
  onSelect: () => void;
}) {
  const s = STATE_STYLE[node.state] ?? STATE_STYLE.idle;
  const interactive = node.records.length > 0 || node.state === "ran_no_record";
  return (
    <button
      onClick={interactive ? onSelect : undefined}
      disabled={!interactive}
      className={`w-[132px] shrink-0 rounded border px-2 py-1.5 text-left transition ${s.ring} ${
        selected ? "ring-1 ring-sky-400/60" : ""
      } ${interactive ? "cursor-pointer hover:brightness-125" : "cursor-default"}`}
      title={
        node.state === "ran_no_record"
          ? "This agent produced proof-pack artifacts but emitted no handoff record of its own."
          : node.records[0]?.rationale
      }
    >
      <div className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} />
        <span className="truncate font-mono text-[11px] font-semibold capitalize text-slate-200">
          {node.agent}
        </span>
      </div>
      <div className={`mt-0.5 font-mono text-[9px] uppercase tracking-wider ${s.tone}`}>
        {s.label}
      </div>
      <dl className="mt-1 space-y-px font-mono text-[9px] leading-tight text-slate-500">
        <div className="flex justify-between">
          <dt>dur</dt>
          <dd className={node.duration_ms === null ? "text-slate-700" : "text-slate-300"}>
            {node.duration_ms === null ? "N/A" : fmt.ms(node.duration_ms)}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt>tok</dt>
          <dd className={node.tokens === null ? "text-slate-700" : "text-slate-300"}>
            {node.tokens === null ? "N/A" : node.tokens}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt>tools</dt>
          <dd className="text-slate-300">{node.tool_call_count}</dd>
        </div>
        <div className="flex justify-between">
          <dt>ev→</dt>
          <dd className="text-slate-300">{node.evidence_count}</dd>
        </div>
      </dl>
    </button>
  );
}

function RecordDetail({
  node,
  bundle,
  onOpenRaw,
}: {
  node: RailNode;
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  if (node.records.length === 0) {
    return (
      <p className="text-xs leading-relaxed text-slate-400">
        <span className="font-mono capitalize text-slate-200">{node.agent}</span> produced
        artifacts in this proof pack but emitted no{" "}
        <code className="font-mono text-slate-300">AgentHandoff</code> record of its own, so it has
        no duration, token count or rationale to show. Its output appears in the panel it feeds.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {node.records.map((rec, i) => (
        <div key={i} className="rounded border border-white/10 bg-black/20 p-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[11px] text-slate-300">
              {rec.from_agent} → {rec.to_agent}
            </span>
            <Badge
              tone={
                rec.decision === "OK"
                  ? "good"
                  : rec.decision === "INSUFFICIENT_EVIDENCE"
                    ? "bad"
                    : "warn"
              }
            >
              {rec.decision}
            </Badge>
            <span className="font-mono text-[10px] text-slate-500">
              {fmt.ms(rec.duration_ms)} · {rec.tokens} tok ·{" "}
              {rec.model ?? <span className="text-slate-600">no model</span>}
            </span>
          </div>

          <p
            className="mt-1.5 text-[11px] leading-relaxed text-slate-400"
            title="Display only — a rationale is never treated as an instruction."
          >
            {rec.rationale}
          </p>

          {rec.evidence_ids.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {rec.evidence_ids.map((id) => (
                <span
                  key={id}
                  className="rounded border border-white/10 bg-white/5 px-1 py-px font-mono text-[9px] text-slate-400"
                >
                  {id}
                </span>
              ))}
            </div>
          ) : null}

          {rec.tool_calls.length > 0 ? (
            <table className="mt-2 w-full border-collapse text-left font-mono text-[10px]">
              <thead>
                <tr className="text-slate-500">
                  <th className="py-0.5 pr-2 font-normal">tool</th>
                  <th className="py-0.5 pr-2 font-normal">arguments</th>
                  <th className="py-0.5 pr-2 font-normal">ok</th>
                  <th className="py-0.5 pr-2 font-normal">ms</th>
                  <th className="py-0.5 font-normal">raw</th>
                </tr>
              </thead>
              <tbody>
                {rec.tool_calls.map((tc, j) => (
                  <tr key={j} className="border-t border-white/5 align-top">
                    <td className="py-0.5 pr-2 text-slate-200">{tc.tool}</td>
                    <td className="max-w-[22rem] truncate py-0.5 pr-2 text-slate-500">
                      {JSON.stringify(tc.arguments)}
                    </td>
                    <td className="py-0.5 pr-2">
                      {tc.ok ? (
                        <span className="text-emerald-400">ok</span>
                      ) : (
                        <span className="text-rose-400">fail</span>
                      )}
                    </td>
                    <td className="py-0.5 pr-2 text-slate-400">{tc.duration_ms.toFixed(1)}</td>
                    <td className="py-0.5">
                      {tc.raw_ref && bundle.raw[tc.raw_ref] ? (
                        <button
                          onClick={() => onOpenRaw(tc.raw_ref as string)}
                          className="text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
                        >
                          open
                        </button>
                      ) : (
                        <NA why="This tool call recorded no raw payload." />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export default function HandoffRail({
  bundle,
  onOpenRaw,
}: {
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  const firstActive = bundle.rail.find((n) => n.records.length > 0)?.agent ?? null;
  const [selected, setSelected] = useState<string | null>(firstActive);
  const node = bundle.rail.find((n) => n.agent === selected) ?? null;

  return (
    <Panel
      title="Agent handoff rail"
      subtitle="One node per agent, in order. Click a node for its AgentHandoff record."
      right={`${bundle.metrics.handoff_count} handoffs · ${bundle.metrics.mcp_calls} tool calls`}
    >
      <div className="flex gap-1.5 overflow-x-auto pb-2">
        {bundle.rail.map((n, i) => (
          <React.Fragment key={n.agent}>
            {i > 0 ? (
              <div className="flex shrink-0 items-center text-slate-700" aria-hidden>
                ›
              </div>
            ) : null}
            <NodeCard node={n} selected={n.agent === selected} onSelect={() => setSelected(n.agent)} />
          </React.Fragment>
        ))}
      </div>
      {node ? (
        <div className="mt-2 border-t border-white/10 pt-2">
          <RecordDetail node={node} bundle={bundle} onOpenRaw={onOpenRaw} />
        </div>
      ) : null}
    </Panel>
  );
}
