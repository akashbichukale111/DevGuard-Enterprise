/**
 * PolicyApproval.tsx — the Command Center: risk, autonomy, the real owner, the diff, and the
 * two independent checks that had to pass before a human was asked.
 *
 * The Command Center requires "the **real DataHub-registered owner** (name, from the graph)"
 * and, when the asset has no owner, that "the fallback behaviour is explicit and
 * displayed". Both are rendered from the Magistrate's captured decision rather
 * than assumed, because an owner invented by the UI would misrepresent who
 * actually approved a production change.
 */

"use client";

import React from "react";
import { fmt, type ReplayBundle } from "@/lib/replay";
import { Badge, Empty, NA, Panel, Urn } from "./primitives";

function Diff({ patch }: { patch: string }) {
  return (
    <pre className="max-h-64 overflow-auto rounded border border-white/10 bg-black/30 p-2 font-mono text-[10px] leading-snug">
      {patch.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---")
          ? "text-slate-500"
          : line.startsWith("+")
            ? "text-emerald-300"
            : line.startsWith("-")
              ? "text-rose-300"
              : line.startsWith("@@")
                ? "text-sky-400"
                : "text-slate-400";
        return (
          <div key={i} className={cls}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

export default function PolicyApproval({
  bundle,
  onOpenRaw,
}: {
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  const p = bundle.policy;
  if (!p) {
    return (
      <Panel title="Policy & approval">
        <Empty>{bundle.missing["policy"] ?? "The run stopped before remediation."}</Empty>
      </Panel>
    );
  }

  const scan = p.sentinel_scan;

  return (
    <Panel
      title="Policy & approval"
      subtitle={p.allowed_action ?? undefined}
      right={
        <div className="flex items-center gap-1">
          {p.risk ? <Badge tone={p.risk === "LOW" ? "good" : "warn"}>{p.risk}</Badge> : null}
          {p.mode ? <Badge tone="info">{p.mode}</Badge> : null}
        </div>
      }
    >
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="space-y-2">
          <div>
            <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
              Registered owner (from the graph)
            </div>
            {p.owners.length === 0 ? (
              <p className="text-[11px] leading-snug text-amber-300/80">
                {p.owner_fallback ?? <NA why={bundle.missing["policy.owners"]} />}
              </p>
            ) : (
              <ul className="space-y-0.5">
                {p.owners.map((o) => (
                  <li key={o.urn} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-xs text-slate-200">{o.display_name}</span>
                    <Urn value={o.urn} />
                    <Badge tone="neutral">{o.ownership_type}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {p.rationale ? (
            <p className="text-[11px] italic leading-snug text-slate-400">{p.rationale}</p>
          ) : null}

          {/*
            No approval record is a state, not a gap. A run whose fix failed
            validation never reached a human, and saying so once is clearer —
            and more accurate — than three stacked N/A fields implying an
            approval that should have been captured and was not.
          */}
          {p.decided_at || p.approver_name || p.approver_urn ? (
            <div className="rounded border border-emerald-500/30 bg-emerald-500/[0.05] p-2">
              <div className="text-[10px] uppercase tracking-[0.12em] text-emerald-300/80">
                Approved by
              </div>
              <div className="text-xs text-slate-100">
                {p.approver_name ?? <NA why="No approver name was recorded." />}
              </div>
              <Urn value={p.approver_urn} />
              <div className="mt-1 font-mono text-[10px] text-slate-500">
                {fmt.time(p.decided_at)}
              </div>
              {p.approval_note ? (
                <p className="mt-1 text-[11px] leading-snug text-slate-400">{p.approval_note}</p>
              ) : null}
              {p.branch_reviewed ? (
                <div className="mt-1 font-mono text-[10px] text-slate-500">
                  branch reviewed: <span className="text-slate-300">{p.branch_reviewed}</span>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="rounded border border-white/10 bg-black/20 p-2">
              <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Approval</div>
              <p className="mt-0.5 text-[11px] leading-snug text-slate-400">
                No approval was requested. The loop did not reach the gate — the proposed fix has
                to pass the Referee before a human is asked, and it did not.
              </p>
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded border border-white/10 bg-black/20 p-2">
              <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
                Sentinel scan
              </div>
              {scan ? (
                <>
                  <div className="mt-0.5 flex items-center gap-1">
                    <Badge tone={scan.blocked ? "bad" : "good"}>
                      {scan.blocked ? "blocked" : "clean"}
                    </Badge>
                    <Badge tone={scan.risk === "LOW" ? "good" : "warn"}>{scan.risk}</Badge>
                  </div>
                  <div className="mt-1 font-mono text-[10px] text-slate-400">
                    +{scan.added} −{scan.removed} · {scan.files_touched.length} file(s) ·{" "}
                    {scan.findings.length} finding(s)
                  </div>
                </>
              ) : (
                <NA why="No patch scan was captured in this run." />
              )}
            </div>

            <div className="rounded border border-white/10 bg-black/20 p-2">
              <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
                Referee validation
              </div>
              {p.validation ? (
                <>
                  <div className="mt-0.5">
                    <Badge tone={/exit code: 0/.test(p.validation) ? "good" : "bad"}>
                      {/exit code: 0/.test(p.validation) ? "builds" : "failed"}
                    </Badge>
                  </div>
                  <button
                    onClick={() => p.validation_ref && onOpenRaw(p.validation_ref)}
                    className="mt-1 font-mono text-[10px] text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
                  >
                    open run output
                  </button>
                </>
              ) : (
                <NA why="No validation output was captured." />
              )}
            </div>
          </div>

          <div>
            <div className="mb-1 flex items-baseline justify-between">
              <span className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
                Proposed diff
              </span>
              {p.proposed_diff_ref ? (
                <button
                  onClick={() => onOpenRaw(p.proposed_diff_ref as string)}
                  className="font-mono text-[10px] text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
                >
                  raw
                </button>
              ) : null}
            </div>
            {p.proposed_diff ? (
              <Diff patch={p.proposed_diff} />
            ) : (
              <Empty>No patch was proposed in this run.</Empty>
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}
