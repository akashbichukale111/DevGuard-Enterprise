"use client";

import {
  DataSourceBadge,
  NotAvailable,
  PanelEmptyState,
  RunMeta,
  Value,
  pickArray,
  pickNumber,
  pickString,
  type PanelProps,
} from "./_shared";

/* -------------------------------------------------------------------------- */
/*  LLMJudgePanel — "Truth Serum" adversarial finding review                   */
/*                                                                            */
/*  Renders the backend's per-finding verdicts verbatim. There is no invented  */
/*  courtroom transcript, confidence sparkline, or trust score: the backend    */
/*  reports findings_reviewed / flagged_count / verdicts[] and this panel      */
/*  shows exactly those, with N/A wherever a field is absent.                  */
/* -------------------------------------------------------------------------- */

/* ---- Shared HUD panel chrome (self-contained) ----------------------------- */

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


/* ---- Main panel ------------------------------------------------------------ */

export default function LLMJudgePanel({ data, status, error, elapsedMs, onRunSingle }: PanelProps) {
  const modelUsed = pickString(data, "model_used");
  const reviewed = pickNumber(data, "findings_reviewed");
  const flagged = pickNumber(data, "flagged_count");
  const recommendedK = pickNumber(data, "recommended_context_k");
  const elevatedK = pickNumber(data, "elevated_context_k");
  const failThreshold = pickNumber(data, "cwe_failure_rate_threshold");
  const verdicts = pickArray(data, "verdicts");

  const hasData = status === "complete" && data !== null;

  return (
    <div
      className="nx-panel relative overflow-hidden rounded-2xl border border-amber-400/25 bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ring-amber-400/20 sm:p-8"
      style={{ ["--panel-glow" as string]: "251, 191, 36" }}
    >
      <NexusPanelStyles />
      <span className="nx-corner nx-corner-tl" />
      <span className="nx-corner nx-corner-tr" />
      <span className="nx-corner nx-corner-bl" />
      <span className="nx-corner nx-corner-br" />

      <div className="flex items-start justify-between gap-4">
        <div>
          <span className="rounded-md border border-amber-400/25 px-2 py-0.5 text-[10px] font-semibold tracking-widest text-amber-300">
            MOD-04
          </span>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">Truth Serum</h2>
          <p className="mt-1 text-xs font-medium uppercase tracking-wider text-amber-300">
            Adversarial finding review
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
          accentClass="text-amber-300"
        />
      ) : (
        <>
          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <JStat label="Findings reviewed" value={reviewed} />
            <JStat label="Flagged" value={flagged} />
            <JStat label="Recommended k" value={recommendedK} />
            <JStat label="Elevated k" value={elevatedK} />
          </div>

          <div className="mt-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-xs">
            <span className="text-white/45">
              Model:{" "}
              <span className="font-mono text-[11px] text-white/80">
                <Value value={modelUsed} />
              </span>
            </span>
            <span className="ml-5 text-white/45">
              CWE fail-rate threshold:{" "}
              <span className="font-mono text-[11px] text-white/80">
                <Value value={failThreshold} decimals={2} />
              </span>
            </span>
          </div>

          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-3 text-[10px] font-medium uppercase tracking-wider text-white/40">
              Per-finding verdicts
            </div>
            <VerdictList rows={verdicts} />
          </div>
        </>
      )}
    </div>
  );
}

function VerdictList({ rows }: { rows: unknown[] | null }) {
  if (!rows || rows.length === 0) {
    return (
      <p className="text-xs text-white/40">
        No verdicts returned for this run — <NotAvailable />
      </p>
    );
  }
  return (
    <ul className="space-y-3">
      {rows.map((raw, i) => {
        const o = (raw ?? {}) as Record<string, unknown>;
        const flagged = o.suspected_hallucination === true;
        return (
          <li
            key={i}
            className={`rounded-lg border p-3 ${
              flagged
                ? "border-rose-400/25 bg-rose-500/[0.05]"
                : "border-emerald-400/20 bg-emerald-500/[0.04]"
            }`}
          >
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px]">
              <span className="text-white/80">
                <Value value={typeof o.cwe_id === "string" ? o.cwe_id : null} />
              </span>
              <span className="text-white/50">
                sev <Value value={typeof o.severity === "string" ? o.severity : null} />
              </span>
              <span className="text-white/50">
                line <Value value={typeof o.line_number === "number" ? o.line_number : null} />
              </span>
              <span className="text-white/50">
                conf{" "}
                <Value
                  value={typeof o.confidence_score === "number" ? o.confidence_score : null}
                  decimals={2}
                />
              </span>
              <span
                className={`ml-auto rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
                  flagged ? "bg-rose-500/15 text-rose-300" : "bg-emerald-500/15 text-emerald-300"
                }`}
              >
                {flagged ? "Flagged" : "Accepted"}
              </span>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-white/60">
              {typeof o.judge_note === "string" ? o.judge_note : <NotAvailable />}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

function JStat({ label, value }: { label: string; value: number | string | null }) {
  return (
    <div className="rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-1.5 text-lg font-semibold text-white">
        <Value value={value} />
      </div>
    </div>
  );
}
