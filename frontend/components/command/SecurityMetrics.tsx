/**
 * SecurityMetrics.tsx — the security panel's security panel and the metrics strip's metrics strip.
 *
 * The security panel reports screening for *this* run. A loop pack that saw no
 * attack says so; it does not borrow the injection demo's detection count
 * from a different pack to make the panel look eventful. "4 sources screened, 0
 * attempts" is the true and useful statement, and the fenced sources are listed
 * so it is checkable.
 *
 * The metrics strip is where the metrics strip's rule bites hardest: "rendered from
 * timings.json. Unknown values render N/A — never a placeholder number." Cost
 * is the live example. No model ran in any captured loop, so cost renders N/A
 * with the reason attached, not $0.00 — the difference between "this was free"
 * and "this was never measured" is exactly what the rule protects.
 */

"use client";

import React from "react";
import { fmt, type ReplayBundle } from "@/lib/replay";
import { Badge, Empty, NA, Panel, Stat } from "./primitives";

export function SecurityPanel({ bundle }: { bundle: ReplayBundle }) {
  const s = bundle.security;
  const detected = s.attempts_detected ?? 0;
  return (
    <Panel
      title="Security — injection screening"
      subtitle="Every catalog-sourced text passes the Sentinel before any prompt"
      tone={detected > 0 ? "alert" : "default"}
      right={
        detected > 0 ? (
          <Badge tone="bad">{detected} attempt(s) detected</Badge>
        ) : (
          <Badge tone="good">no attempt detected</Badge>
        )
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <Stat
          label="Text sources scanned"
          value={s.sources_scanned === null ? "N/A" : String(s.sources_scanned)}
          na={s.sources_scanned === null}
        />
        <Stat
          label="Attempts detected"
          value={s.attempts_detected === null ? "N/A" : String(s.attempts_detected)}
          na={s.attempts_detected === null}
        />
      </div>

      {s.action_taken ? (
        <p className="mt-2 text-[11px] leading-relaxed text-slate-400">{s.action_taken}</p>
      ) : (
        <Empty>No screening was recorded in this run.</Empty>
      )}

      {s.untrusted_sources.length > 0 ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-[10px] uppercase tracking-[0.12em] text-slate-500">
            {s.untrusted_sources.length} fenced source(s)
          </summary>
          <ul className="mt-1 space-y-0.5">
            {s.untrusted_sources.map((n) => (
              <li key={n} className="break-all font-mono text-[10px] text-fuchsia-300/70">
                {n}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <p className="mt-2 border-t border-white/10 pt-2 text-[10px] italic leading-snug text-slate-500">
        The Diagnostician — the agent that reads this text — has no tools at all, so injected text
        cannot cause a tool call even if it is never detected.
      </p>
    </Panel>
  );
}

export function MetricsStrip({ bundle }: { bundle: ReplayBundle }) {
  const m = bundle.metrics;
  return (
    <Panel
      title="Metrics"
      subtitle={m.source}
      right={
        m.models.length > 0 ? (
          <Badge tone="info">{m.models.join(", ")}</Badge>
        ) : (
          <Badge tone="warn" title={bundle.missing["metrics.cost_usd"]}>
            no model invoked
          </Badge>
        )
      }
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 xl:grid-cols-6">
        <Stat
          label="Time to root cause"
          value={fmt.ms(m.time_to_root_cause_ms)}
          na={m.time_to_root_cause_ms === null}
          hint={m.time_to_root_cause_ms === null ? bundle.missing["metrics.time_to_root_cause_ms"] : null}
        />
        <Stat label="Total loop" value={fmt.ms(m.total_loop_ms)} na={m.total_loop_ms === null} />
        <Stat label="MCP calls" value={fmt.int(m.mcp_calls)} na={m.mcp_calls === null} />
        <Stat
          label="Tokens"
          value={fmt.int(m.tokens)}
          na={m.tokens === null}
          hint={m.tokens_note}
        />
        <Stat
          label="Cost"
          value={fmt.usd(m.cost_usd)}
          na={m.cost_usd === null}
          hint={m.cost_usd === null ? "unmeasured — not zero" : null}
        />
        <Stat label="Evidence items" value={fmt.int(m.evidence_count)} />
      </div>

      {m.cost_usd === null ? (
        <p className="mt-2 border-t border-white/10 pt-2 text-[10px] leading-snug text-slate-500">
          <span className="text-slate-400">Why N/A:</span>{" "}
          {bundle.missing["metrics.cost_usd"] ?? "not measured in this run"}
        </p>
      ) : null}
    </Panel>
  );
}

/** Anything the bundle could not fill in, listed once so nothing hides. */
export function MissingLedger({ bundle }: { bundle: ReplayBundle }) {
  const entries = Object.entries(bundle.missing);
  if (entries.length === 0) return null;
  return (
    <Panel
      title="What this run did not capture"
      subtitle="Every N/A on this screen, with the reason it was not measured"
      right={<Badge tone="neutral">{entries.length}</Badge>}
    >
      <dl className="space-y-1">
        {entries.map(([field, why]) => (
          <div key={field} className="grid gap-1 sm:grid-cols-[14rem_1fr]">
            <dt className="font-mono text-[10px] text-slate-400">{field}</dt>
            <dd className="text-[11px] leading-snug text-slate-500">{why}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}
