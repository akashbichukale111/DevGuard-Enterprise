"use client";

import { motion } from "framer-motion";
import {
  DataSourceBadge,
  NotAvailable,
  PanelEmptyState,
  RunMeta,
  Value,
  pickArray,
  pickNumber,
  pickObject,
  pickString,
  type PanelProps,
} from "./_shared";

/* -------------------------------------------------------------------------- */
/*  PreCogPanel — "Future Predictor" visual module                            */
/*                                                                            */
/*  Two custom SVG/CSS visualizations (no charting lib, so every beat can be   */
/*  choreographed): a radar graph whose projected line bends away from a red   */
/*  "crash zone" right before intercept, and a blast-radius map built from     */
/*  concentric pulse rings + trig-positioned nodes around a center service.    */
/*  Both intentionally skip Monaco/DiffEditor — this module's "proof" is the   */
/*  forecast graphics, with the fix indicators as compact after-the-fact       */
/*  confirmation strips rather than another full diff viewer.                 */
/* -------------------------------------------------------------------------- */

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

/* ---- Main panel ---------------------------------------------------------------- */

export default function PreCogPanel({ data, status, error, elapsedMs, onRunSingle }: PanelProps) {
  const currentErrorRate = pickNumber(data, "current_error_rate_pct");
  const tripThreshold = pickNumber(data, "circuit_breaker_trip_threshold_pct");
  const minutesToTrip = pickNumber(data, "projected_minutes_to_breaker_trip");
  const autoscale = pickString(data, "autoscale_recommendation");
  const forecast = pickArray(data, "error_rate_forecast");

  const mem = pickObject(data, "memory_forecast");
  const num = (o: Record<string, unknown> | null, k: string) =>
    typeof o?.[k] === "number" ? (o[k] as number) : null;
  const startingRss = num(mem, "starting_rss_mb");
  const leakRate = num(mem, "leak_rate_mb_per_min");
  const oomCeiling = num(mem, "oom_ceiling_mb");
  const minutesToOom = num(mem, "projected_minutes_to_oom");
  // "measured" when the backend read this process's real RSS; "synthetic" when
  // /proc was unreadable and it fell back. Never assume — the field is absent on
  // an older backend, and absent must not read as measured.
  const rssSource = typeof mem?.["current_rss_source"] === "string"
    ? (mem["current_rss_source"] as string)
    : null;

  const hasData = status === "complete" && data !== null;

  return (
    <div
      className="nx-panel relative overflow-hidden rounded-2xl border border-violet-400/25 bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ring-violet-400/20 sm:p-8"
      style={{ ["--panel-glow" as string]: "167, 139, 250" }}
    >
      <NexusPanelStyles />
      <span className="nx-corner nx-corner-tl" />
      <span className="nx-corner nx-corner-tr" />
      <span className="nx-corner nx-corner-bl" />
      <span className="nx-corner nx-corner-br" />

      <div className="flex items-start justify-between gap-4">
        <div>
          <span className="rounded-md border border-violet-400/25 px-2 py-0.5 text-[10px] font-semibold tracking-widest text-violet-300">
            MOD-03
          </span>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">Pre-Cog Ops</h2>
          <p className="mt-1 text-xs font-medium uppercase tracking-wider text-violet-300">
            Predictive failure forecasting
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
          accentClass="text-violet-300"
        />
      ) : (
        <>
          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <PcStat label="Current error rate" value={currentErrorRate} suffix="%" decimals={2} />
            <PcStat label="Breaker threshold" value={tripThreshold} suffix="%" decimals={1} />
            <PcStat
              label="Projected trip"
              value={minutesToTrip}
              suffix={minutesToTrip === null ? "" : " min"}
            />
            <PcStat label="Autoscale" value={autoscale} />
          </div>

          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-3 text-[10px] font-medium uppercase tracking-wider text-white/40">
              Error-rate forecast &middot; projected vs breaker threshold
            </div>
            <ForecastChart points={forecast} threshold={tripThreshold} />
          </div>

          {/* The four figures here have DIFFERENT origins, so the panel labels
              them individually. The backend used to badge the whole payload
              "live" whenever the error rate came from telemetry, while all four
              of these were random.uniform draws. Current RSS is now a real
              reading of the process; the leak rate is a scenario parameter, and
              the OOM projection is derived from it — so both say so rather than
              inheriting the panel's badge. */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">
                Memory forecast
              </div>
              <div className="text-[10px] text-white/30">
                current RSS {rssSource === "measured" ? "measured" : "simulated"} ·
                leak rate simulated
              </div>
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2.5 text-xs sm:grid-cols-4">
              <MemStat
                label={rssSource === "measured" ? "Current RSS (measured)" : "Current RSS (sim)"}
                value={startingRss} suffix=" MB" decimals={1}
              />
              <MemStat label="Leak rate (sim)" value={leakRate} suffix=" MB/min" decimals={2} />
              <MemStat label="OOM ceiling" value={oomCeiling} suffix=" MB" decimals={0} />
              <MemStat label="Projected OOM (sim)" value={minutesToOom} suffix=" min" decimals={1} />
            </dl>
          </div>
        </>
      )}
    </div>
  );
}

/* ---- Forecast chart — plotted from error_rate_forecast, nothing invented -- */

function ForecastChart({
  points,
  threshold,
}: {
  points: unknown[] | null;
  threshold: number | null;
}) {
  if (!points || points.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-white/10">
        <span className="text-xs text-white/40">
          No forecast returned for this run — <NotAvailable />
        </span>
      </div>
    );
  }

  const series = points.map((raw) => {
    const o = (raw ?? {}) as Record<string, unknown>;
    return {
      minute: typeof o.minute === "number" ? o.minute : null,
      value:
        typeof o.projected_error_rate_pct === "number"
          ? (o.projected_error_rate_pct as number)
          : null,
    };
  });

  const values = series.map((p) => p.value).filter((v): v is number => v !== null);
  if (values.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-white/10">
        <span className="text-xs text-white/40">
          Forecast contained no numeric points — <NotAvailable />
        </span>
      </div>
    );
  }

  const width = 560;
  const height = 160;
  const max = Math.max(...values, threshold ?? 0) * 1.1 || 1;
  const stepX = series.length > 1 ? width / (series.length - 1) : width;

  const pts = series.map((p, i) => {
    const y = height - ((p.value ?? 0) / max) * (height - 20) - 10;
    return [i * stepX, y] as const;
  });
  const linePath = pts
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const thresholdY =
    threshold !== null ? height - (threshold / max) * (height - 20) - 10 : null;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-40 w-full overflow-visible">
      {thresholdY !== null && (
        <>
          <line
            x1={0}
            x2={width}
            y1={thresholdY}
            y2={thresholdY}
            stroke="rgba(251,113,133,0.55)"
            strokeDasharray="4 4"
            strokeWidth={1}
          />
          <text
            x={width - 2}
            y={thresholdY - 6}
            textAnchor="end"
            fontSize="9"
            fill="rgba(251,113,133,0.8)"
          >
            breaker {threshold}%
          </text>
        </>
      )}
      <motion.path
        d={linePath}
        fill="none"
        stroke="#a78bfa"
        strokeWidth={2}
        strokeLinecap="round"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 1.4, ease: [0.22, 1, 0.36, 1] }}
      />
    </svg>
  );
}

function PcStat({
  label,
  value,
  suffix = "",
  decimals,
}: {
  label: string;
  value: number | string | null;
  suffix?: string;
  decimals?: number;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-1.5 text-lg font-semibold text-white">
        <Value value={value} suffix={suffix} decimals={decimals} />
      </div>
    </div>
  );
}

function MemStat({
  label,
  value,
  suffix,
  decimals,
}: {
  label: string;
  value: number | null;
  suffix: string;
  decimals: number;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-white/40">{label}</dt>
      <dd className="mt-1 font-mono text-sm text-white/80">
        <Value value={value} suffix={suffix} decimals={decimals} />
      </dd>
    </div>
  );
}
