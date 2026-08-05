"use client";

import { motion } from "framer-motion";
import {
  DataSourceBadge,
  NotAvailable,
  PanelEmptyState,
  RunMeta,
  Value,
  pickBool,
  pickNumber,
  pickObject,
  pickString,
  type PanelProps,
} from "./_shared";

/* -------------------------------------------------------------------------- */
/*  FinOpsPanel — "Cost Controller" visual module                             */
/*                                                                            */
/*  The cost/ingestion graph is a hand-drawn SVG path (not a charting lib) so  */
/*  it can be choreographed exactly: spike -> detection marker -> drop -> flat */
/*  line, animated with a single pathLength tween. The YAML diff reuses the    */
/*  Monaco DiffEditor pattern from app/result/page.tsx so the "before/after    */
/*  config" moment reads with the same visual authority as the code diff on   */
/*  the Result Dashboard.                                                     */
/* -------------------------------------------------------------------------- */

/* ---- Spend vs budget ------------------------------------------------------
   The backend reports two scalars for the 30-minute window — spend and budget.
   It does not report a time series, so this renders a two-bar comparison of
   the values that actually exist rather than the invented 13-point sparkline
   that used to live here. */

function SpendVsBudget({ spend, budget }: { spend: number | null; budget: number | null }) {
  if (spend === null || budget === null) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-white/10">
        <span className="text-xs text-white/40">
          Spend / budget not reported for this run — <NotAvailable />
        </span>
      </div>
    );
  }

  const scale = Math.max(spend, budget) || 1;
  const spendPct = (spend / scale) * 100;
  const budgetPct = (budget / scale) * 100;
  const overBudget = spend > budget;

  return (
    <div className="flex h-40 flex-col justify-center gap-5 px-1">
      <div>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="text-[10px] font-medium uppercase tracking-wider text-white/45">
            Spend · last 30 min
          </span>
          <span
            className={`font-mono text-sm font-semibold ${
              overBudget ? "text-rose-300" : "text-emerald-300"
            }`}
          >
            ${spend.toFixed(4)}
          </span>
        </div>
        <div className="h-2.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
          <motion.div
            className={`h-full rounded-full ${overBudget ? "bg-rose-400" : "bg-emerald-400"}`}
            initial={{ width: 0 }}
            animate={{ width: `${spendPct}%` }}
            transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
          />
        </div>
      </div>

      <div>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="text-[10px] font-medium uppercase tracking-wider text-white/45">
            Budget · per 30 min
          </span>
          <span className="font-mono text-sm font-semibold text-amber-300">
            ${budget.toFixed(2)}
          </span>
        </div>
        <div className="h-2.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
          <motion.div
            className="h-full rounded-full bg-amber-400/70"
            initial={{ width: 0 }}
            animate={{ width: `${budgetPct}%` }}
            transition={{ duration: 1.1, delay: 0.15, ease: [0.22, 1, 0.36, 1] }}
          />
        </div>
      </div>

      <div
        className={`text-xs font-medium ${overBudget ? "text-rose-300" : "text-emerald-300"}`}
      >
        {overBudget ? "Over budget" : "Within budget"}
      </div>
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
      .nx-roi-glow {
        box-shadow: 0 0 0 1px rgba(52, 211, 153, 0.1), 0 0 40px -12px rgba(52, 211, 153, 0.45);
      }
    `}</style>
  );
}

/* ---- Main panel -------------------------------------------------------------- */

export default function FinOpsPanel({ data, status, error, elapsedMs, onRunSingle }: PanelProps) {
  const spend = pickNumber(data, "spend_usd_per_30min");
  const budget = pickNumber(data, "budget_usd_per_30min");
  const overBudget = pickBool(data, "over_budget");
  const conservation = pickBool(data, "conservation_mode_active");
  const tier = pickString(data, "model_tier_recommendation");
  const reasoning = pickString(data, "reasoning");
  const autoApply = pickBool(data, "auto_apply");

  const sampling = pickObject(data, "sampling_config");
  const currentRatio =
    typeof sampling?.current_head_sampling_ratio === "number"
      ? (sampling.current_head_sampling_ratio as number)
      : null;
  const recommendedRatio =
    typeof sampling?.recommended_head_sampling_ratio === "number"
      ? (sampling.recommended_head_sampling_ratio as number)
      : null;
  const configTarget =
    typeof sampling?.config_target === "string" ? (sampling.config_target as string) : null;

  const hasData = status === "complete" && data !== null;

  return (
    <div
      className="nx-panel relative overflow-hidden rounded-2xl border border-emerald-400/25 bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ring-emerald-400/20 sm:p-8"
      style={{ ["--panel-glow" as string]: "52, 211, 153" }}
    >
      <NexusPanelStyles />
      <span className="nx-corner nx-corner-tl" />
      <span className="nx-corner nx-corner-tr" />
      <span className="nx-corner nx-corner-bl" />
      <span className="nx-corner nx-corner-br" />

      <div className="flex items-start justify-between gap-4">
        <div>
          <span className="rounded-md border border-emerald-400/25 px-2 py-0.5 text-[10px] font-semibold tracking-widest text-emerald-300">
            MOD-02
          </span>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">FinOps Agent</h2>
          <p className="mt-1 text-xs font-medium uppercase tracking-wider text-emerald-300">
            Autonomous cost controller
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
          accentClass="text-emerald-300"
        />
      ) : (
        <>
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-white/40">
              LLM spend vs budget &middot; last 30 minutes
            </div>
            <SpendVsBudget spend={spend} budget={budget} />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Sampling recommendation — real values only */}
            <div className="rounded-xl border border-white/10 bg-black/20 p-5">
              <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">
                Head sampling ratio
              </div>
              <div className="mt-3 flex items-baseline gap-3">
                <Value value={currentRatio} decimals={2} className="text-2xl font-semibold text-white" />
                <span className="text-white/30">&rarr;</span>
                <Value
                  value={recommendedRatio}
                  decimals={2}
                  className="text-2xl font-semibold text-emerald-300"
                />
              </div>
              <dl className="mt-4 space-y-2 text-xs">
                <div className="flex justify-between gap-4">
                  <dt className="text-white/45">Config target</dt>
                  <dd className="text-right font-mono text-[11px] text-white/70">
                    <Value value={configTarget} />
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-white/45">Auto-apply</dt>
                  <dd className="text-white/70">
                    {autoApply === null ? <NotAvailable /> : autoApply ? "Yes" : "No"}
                  </dd>
                </div>
              </dl>
            </div>

            {/* Routing state — real values only */}
            <div className="rounded-xl border border-white/10 bg-black/20 p-5">
              <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">
                Routing state
              </div>
              <dl className="mt-3 space-y-2.5 text-xs">
                <div className="flex justify-between gap-4">
                  <dt className="text-white/45">Recommended tier</dt>
                  <dd className="text-right font-mono text-[11px] text-white/80">
                    <Value value={tier} />
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-white/45">Over budget</dt>
                  <dd className="text-white/80">
                    {overBudget === null ? <NotAvailable /> : overBudget ? "Yes" : "No"}
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-white/45">Conservation mode</dt>
                  <dd className="text-white/80">
                    {conservation === null ? (
                      <NotAvailable />
                    ) : conservation ? (
                      "Active"
                    ) : (
                      "Inactive"
                    )}
                  </dd>
                </div>
              </dl>
            </div>
          </div>

          {/* The agent's own stated reasoning, rendered verbatim */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">
              Agent reasoning
            </div>
            <p className="mt-2 font-mono text-xs leading-relaxed text-white/70">
              {reasoning ?? <NotAvailable />}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
