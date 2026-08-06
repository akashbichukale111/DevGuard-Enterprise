"use client";

import { DiffEditor } from "@monaco-editor/react";

// Side-effect import: self-hosted Monaco loader path. See lib/monaco.ts.
import "@/lib/monaco";
import {
  DataSourceBadge,
  NotAvailable,
  PanelEmptyState,
  RunMeta,
  Value,
  pickArray,
  pickBool,
  pickNumber,
  pickString,
  type PanelProps,
} from "./_shared";

/* -------------------------------------------------------------------------- */
/*  OmniHealPanel — "AI Auto-Fixer" visual module                             */
/*                                                                            */
/*  Reuses the same Monaco DiffEditor pattern as app/result/page.tsx (dark    */
/*  theme, readOnly, side-by-side, no minimap) so the diff here reads exactly */
/*  like the one judges already saw on the Result Dashboard — same visual     */
/*  grammar, new context. Everything else (trace graph, terminal, Slack       */
/*  card) is new chrome built to match that page's glassmorphic language.     */
/* -------------------------------------------------------------------------- */

interface VulnRow {
  cwe_id?: unknown;
  severity?: unknown;
  line_number?: unknown;
  confidence_score?: unknown;
}

/* ---- Findings table — rendered from vulnerabilities_found ---------------- */

function FindingsTable({ rows }: { rows: unknown[] | null }) {
  if (!rows || rows.length === 0) {
    return (
      <p className="text-xs text-white/40">
        No vulnerabilities reported for this run — <NotAvailable />
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-white/35">
            <th className="pb-2 pr-4 font-medium">CWE</th>
            <th className="pb-2 pr-4 font-medium">Severity</th>
            <th className="pb-2 pr-4 font-medium">Line</th>
            <th className="pb-2 font-medium">Confidence</th>
          </tr>
        </thead>
        <tbody className="font-mono text-[11px]">
          {rows.map((raw, i) => {
            const r = (raw ?? {}) as VulnRow;
            const sev = typeof r.severity === "string" ? r.severity : null;
            return (
              <tr key={i} className="border-t border-white/[0.06]">
                <td className="py-2 pr-4 text-white/80">
                  <Value value={typeof r.cwe_id === "string" ? r.cwe_id : null} />
                </td>
                <td className="py-2 pr-4">
                  <span
                    className={
                      sev === "critical" || sev === "high"
                        ? "text-rose-300"
                        : sev === "medium"
                        ? "text-amber-300"
                        : "text-white/70"
                    }
                  >
                    <Value value={sev} />
                  </span>
                </td>
                <td className="py-2 pr-4 text-white/70">
                  <Value value={typeof r.line_number === "number" ? r.line_number : null} />
                </td>
                <td className="py-2 text-white/70">
                  <Value
                    value={typeof r.confidence_score === "number" ? r.confidence_score : null}
                    decimals={2}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ---- Shared HUD panel chrome (self-contained — no dependency on any other
   page/component's global styles, so this component drops in standalone) --- */

function NexusPanelStyles() {
  return (
    <style jsx global>{`
      .nx-panel {
        box-shadow: 0 0 0 1px rgba(var(--panel-glow), 0.06), 0 0 50px -18px rgba(var(--panel-glow), 0.35),
          0 20px 60px -24px rgba(0, 0, 0, 0.8), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      }
      .nx-corner {
        position: absolute;
        width: 18px;
        height: 18px;
        border-color: rgba(var(--panel-glow), 0.55);
        pointer-events: none;
      }
      .nx-corner-tl {
        top: 10px;
        left: 10px;
        border-top: 2px solid;
        border-left: 2px solid;
        border-radius: 4px 0 0 0;
      }
      .nx-corner-tr {
        top: 10px;
        right: 10px;
        border-top: 2px solid;
        border-right: 2px solid;
        border-radius: 0 4px 0 0;
      }
      .nx-corner-bl {
        bottom: 10px;
        left: 10px;
        border-bottom: 2px solid;
        border-left: 2px solid;
        border-radius: 0 0 0 4px;
      }
      .nx-corner-br {
        bottom: 10px;
        right: 10px;
        border-bottom: 2px solid;
        border-right: 2px solid;
        border-radius: 0 0 4px 0;
      }
    `}</style>
  );
}

/* ---- Main panel ----------------------------------------------------------- */

export default function OmniHealPanel({ data, status, error, elapsedMs, onRunSingle }: PanelProps) {
  const originalCode = pickString(data, "original_code");
  const patchedCode = pickString(data, "patched_code");
  const diffSummary = pickString(data, "diff_summary");
  const evalScore = pickNumber(data, "eval_score");
  const verdict = pickString(data, "verdict");
  const converged = pickBool(data, "converged");
  const attempts = pickNumber(data, "reflection_attempts");
  const maxRetries = pickNumber(data, "max_reflection_retries");
  const modelUsed = pickString(data, "model_used_for_fix");
  const vulns = pickArray(data, "vulnerabilities_found");
  const addressed = pickArray(data, "addressed_cwe_ids");

  const cweLabel =
    addressed && addressed.length > 0 && typeof addressed[0] === "string"
      ? (addressed[0] as string)
      : null;

  const hasData = status === "complete" && data !== null;

  return (
    <div
      className="nx-panel relative overflow-hidden rounded-2xl border border-cyan-400/25 bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ring-cyan-400/20 sm:p-8"
      style={{ ["--panel-glow" as string]: "34, 211, 238" }}
    >
      <NexusPanelStyles />
      <span className="nx-corner nx-corner-tl" />
      <span className="nx-corner nx-corner-tr" />
      <span className="nx-corner nx-corner-bl" />
      <span className="nx-corner nx-corner-br" />

      <div className="flex items-start justify-between gap-4">
        <div>
          <span className="rounded-md border border-cyan-400/25 px-2 py-0.5 text-[10px] font-semibold tracking-widest text-cyan-300">
            MOD-01
          </span>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">Omni-Heal</h2>
          <p className="mt-1 text-xs font-medium uppercase tracking-wider text-cyan-300">
            AI Auto-Fixer &middot; Scanner &rarr; Fixer &rarr; Validator
          </p>
          <RunMeta data={data} elapsedMs={elapsedMs} />
        </div>
        <DataSourceBadge data={data} status={status} />
      </div>

      {!hasData ? (
        <PanelEmptyState
          status={status}
          error={error}
          onRunSingle={onRunSingle}
          accentClass="text-cyan-300"
        />
      ) : (
        <>
          {/* Pipeline outcome — every value read straight off the response */}
          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Addressed CWE" value={cweLabel} />
            <Stat label="Eval score" value={evalScore} suffix={evalScore === null ? "" : "/100"} />
            <Stat label="Verdict" value={verdict} />
            <Stat
              label="Reflection attempts"
              value={
                attempts === null
                  ? null
                  : maxRetries === null
                  ? String(attempts)
                  : `${attempts}/${maxRetries}`
              }
            />
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-xs">
            <span className="text-white/45">
              Converged:{" "}
              <span className="text-white/80">
                {converged === null ? <NotAvailable /> : converged ? "Yes" : "No"}
              </span>
            </span>
            <span className="text-white/45">
              Model: <span className="font-mono text-[11px] text-white/80"><Value value={modelUsed} /></span>
            </span>
          </div>

          {/* Findings */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-3 text-[10px] font-medium uppercase tracking-wider text-white/40">
              Vulnerabilities found
            </div>
            <FindingsTable rows={vulns} />
          </div>

          {/* Fix summary, verbatim from the agent */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">
              Diff summary
            </div>
            <p className="mt-2 font-mono text-xs leading-relaxed text-white/70">
              {diffSummary ?? <NotAvailable />}
            </p>
          </div>

          {/* Code diff — real original vs real patched */}
          <div className="mt-6 overflow-hidden rounded-xl border border-white/10">
            <div className="flex items-center justify-between border-b border-white/10 bg-white/5 px-4 py-2.5">
              <span className="text-xs font-medium text-white/70">
                Patch &middot; original vs. AI-fixed
              </span>
              <div className="flex items-center gap-3 text-[10px] font-medium">
                <span className="flex items-center gap-1 text-rose-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-rose-400" /> vulnerable
                </span>
                <span className="flex items-center gap-1 text-emerald-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> patched
                </span>
              </div>
            </div>
            {originalCode === null && patchedCode === null ? (
              <div className="flex h-24 items-center justify-center">
                <span className="text-xs text-white/40">
                  No code returned for this run — <NotAvailable />
                </span>
              </div>
            ) : (
              <div className="h-64">
                <DiffEditor
                  original={originalCode ?? ""}
                  modified={patchedCode ?? ""}
                  language="python"
                  theme="vs-dark"
                  options={{
                    readOnly: true,
                    renderSideBySide: true,
                    minimap: { enabled: false },
                    fontSize: 13,
                    scrollBeyondLastLine: false,
                    glyphMargin: true,
                  }}
                />
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  suffix = "",
}: {
  label: string;
  value: number | string | null;
  suffix?: string;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-1.5 text-lg font-semibold text-white">
        <Value value={value} suffix={suffix} />
      </div>
    </div>
  );
}
