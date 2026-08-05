/**
 * IncidentHeader.tsx — the Command Center: the header strip and the always-visible mode banners.
 *
 * The banners are not decoration. The Command Center requires `REPLAY OF RECORDED RUN — NOT
 * LIVE` whenever the UI is not driven by a live loop, and the replay build is
 * never live, so it renders unconditionally and cannot be dismissed. A judge
 * must never be able to mistake a recorded run for a live one.
 *
 * The elapsed clock is the recorded wall-clock span of the run, not a ticking
 * timer. A replay of a 14-second incident that appears to still be running
 * would be a fabricated live state.
 */

"use client";

import React from "react";
import { fmt, type ReplayBundle } from "@/lib/replay";
import { Badge, NA, Urn } from "./primitives";

const SEVERITY_TONE: Record<string, "good" | "warn" | "bad" | "neutral"> = {
  LOW: "good",
  MEDIUM: "warn",
  HIGH: "bad",
  CRITICAL: "bad",
};

export function ModeBanners({ bundle }: { bundle: ReplayBundle }) {
  const b = bundle.banners;
  return (
    <div className="flex flex-wrap gap-2">
      {b.replay ? (
        <div className="flex-1 rounded border border-amber-400/50 bg-amber-400/10 px-3 py-1.5 text-center font-mono text-[11px] font-bold uppercase tracking-[0.2em] text-amber-300">
          {b.replay_text}
        </div>
      ) : null}
      {b.dry_run ? (
        <div className="flex-1 rounded border border-sky-400/50 bg-sky-400/10 px-3 py-1.5 text-center font-mono text-[11px] font-bold uppercase tracking-[0.2em] text-sky-300">
          DRY RUN — NOTHING WAS WRITTEN
        </div>
      ) : null}
      {b.seeded_catalog ? (
        <div className="flex-1 rounded border border-fuchsia-400/50 bg-fuchsia-400/10 px-3 py-1.5 text-center font-mono text-[11px] font-bold uppercase tracking-[0.2em] text-fuchsia-300">
          SEEDED CATALOG CONTEXT
        </div>
      ) : null}
    </div>
  );
}

export function StateMachine({
  states,
  reached,
  current,
}: {
  states: string[];
  reached: string[];
  current: string | null;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {states.map((s, i) => {
        const hit = reached.includes(s);
        const isCurrent = s === current;
        return (
          <React.Fragment key={s}>
            {i > 0 ? <span className="text-slate-700">›</span> : null}
            <span
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] tracking-wide ${
                isCurrent
                  ? "bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-400/50"
                  : hit
                    ? "text-emerald-400/70"
                    : "text-slate-700"
              }`}
              title={hit ? "reached in this run" : "not reached in this run"}
            >
              {s}
            </span>
          </React.Fragment>
        );
      })}
    </div>
  );
}

export default function IncidentHeader({ bundle }: { bundle: ReplayBundle }) {
  const inc = bundle.incident;
  const sevTone = inc.severity ? (SEVERITY_TONE[inc.severity] ?? "neutral") : "neutral";
  return (
    <header className="space-y-2">
      <ModeBanners bundle={bundle} />
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded border border-white/10 bg-[#0f0f16] px-3 py-2">
        <div>
          <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Incident</div>
          <div className="font-mono text-base font-semibold text-slate-100">
            {inc.incident_id ?? <NA why={bundle.missing["incident.incident_id"]} />}
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Affected dataset
          </div>
          <div className="truncate">
            <Urn value={inc.dataset_urn} href={inc.datahub_link} />
            {!inc.datahub_link && inc.dataset_urn ? (
              <span
                className="ml-2 text-[10px] text-slate-600"
                title="The bundle was built without --datahub-url, so there is no host to link to. The URN is shown in full."
              >
                (no DataHub link in this bundle)
              </span>
            ) : null}
          </div>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Severity</div>
          {inc.severity ? (
            <Badge tone={sevTone}>{inc.severity}</Badge>
          ) : (
            <NA why={bundle.missing["incident.severity"]} />
          )}
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Elapsed</div>
          <div
            className="font-mono text-base text-slate-100"
            title={`${fmt.time(inc.started_at)} → ${fmt.time(inc.ended_at)} (recorded, not live)`}
          >
            {inc.elapsed_ms === null ? (
              <NA why={bundle.missing["incident.elapsed_ms"]} />
            ) : (
              fmt.ms(inc.elapsed_ms)
            )}
          </div>
        </div>
      </div>

      <div className="rounded border border-white/10 bg-[#0f0f16] px-3 py-2">
        <StateMachine states={inc.states} reached={inc.states_reached} current={inc.state} />
      </div>
    </header>
  );
}
