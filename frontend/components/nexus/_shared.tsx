"use client";

/* -------------------------------------------------------------------------- */
/*  Shared Nexus panel primitives                                             */
/*                                                                            */
/*  These exist to enforce the no-fabrication rule / the provenance rule at the component level rather than   */
/*  by convention:                                                            */
/*                                                                            */
/*   - `pick*` read a single field off the raw backend response and return    */
/*     `null` when it is absent. They never substitute a fallback value, so   */
/*     a missing field can only ever render as N/A.                           */
/*   - `<Value>` is the only sanctioned way to print a machine number. If it  */
/*     is handed `null` it renders the N/A treatment.                         */
/*   - `<DataSourceBadge>` surfaces the backend's own `data_source` field.    */
/*                                                                            */
/*  The backend speaks snake_case (FastAPI); these helpers read that shape    */
/*  directly rather than reshaping into camelCase, which is what previously   */
/*  caused every real value to be dropped silently on the floor.              */
/* -------------------------------------------------------------------------- */

export type PanelStatus = "idle" | "executing" | "complete" | "error";

/** Raw, unmapped backend response. Deliberately not a rich type: the panels
 *  read named fields through the `pick*` helpers, which handle absence. */
export type RawResponse = Record<string, unknown> | null;

export interface PanelProps {
  data: RawResponse;
  status: PanelStatus;
  error: string | null;
  elapsedMs: number | null;
  onRunSingle?: () => void;
}

/* ---- Field readers — return null rather than a substitute ---------------- */

export function pickNumber(data: RawResponse, key: string): number | null {
  const v = data?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function pickString(data: RawResponse, key: string): string | null {
  const v = data?.[key];
  return typeof v === "string" && v.length > 0 ? v : null;
}

export function pickBool(data: RawResponse, key: string): boolean | null {
  const v = data?.[key];
  return typeof v === "boolean" ? v : null;
}

export function pickArray(data: RawResponse, key: string): unknown[] | null {
  const v = data?.[key];
  return Array.isArray(v) ? v : null;
}

export function pickObject(data: RawResponse, key: string): Record<string, unknown> | null {
  const v = data?.[key];
  return typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

/* ---- N/A-safe value rendering ------------------------------------------- */

export function Value({
  value,
  prefix = "",
  suffix = "",
  decimals,
  className = "",
}: {
  value: number | string | null;
  prefix?: string;
  suffix?: string;
  decimals?: number;
  className?: string;
}) {
  if (value === null) return <NotAvailable className={className} />;
  const body =
    typeof value === "number" && decimals !== undefined ? value.toFixed(decimals) : String(value);
  return (
    <span className={`tabular-nums ${className}`}>
      {prefix}
      {body}
      {suffix}
    </span>
  );
}

export function NotAvailable({ className = "" }: { className?: string }) {
  return (
    <span
      title="Not reported by the backend for this run"
      className={`font-mono text-white/35 ${className}`}
    >
      N/A
    </span>
  );
}

/* ---- data_source badge --------------------------------------------------- */

/** Renders the backend's own `data_source`. Colour is never the only signal —
 *  the word is always present, so it survives video compression and is
 *  readable without colour vision. */
export function DataSourceBadge({ data, status }: { data: RawResponse; status: PanelStatus }) {
  if (status === "idle") {
    return (
      <span className="rounded-full border border-white/15 bg-white/[0.04] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-white/45">
        No run yet
      </span>
    );
  }
  if (status === "executing") {
    return (
      <span className="rounded-full border border-cyan-400/30 bg-cyan-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-cyan-300">
        Executing
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="rounded-full border border-rose-400/30 bg-rose-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-rose-300">
        Error
      </span>
    );
  }

  const source = pickString(data, "data_source");
  if (source === "live") {
    return (
      <span className="rounded-full border border-emerald-400/30 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
        Live
      </span>
    );
  }
  if (source === "synthetic") {
    return (
      <span className="rounded-full border border-amber-400/30 bg-amber-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
        Simulated
      </span>
    );
  }
  if (source === "local_shadow") {
    // Real measurement of this process, but computed by an in-process
    // heuristic rather than retrieved from SigNoz. Deliberately not badged
    // "Live" — that distinction is the whole point of the label.
    return (
      <span
        title="Measured in-process by DevGuard, not retrieved from SigNoz"
        className="rounded-full border border-sky-400/30 bg-sky-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-sky-300"
      >
        Local
      </span>
    );
  }
  if (source === "partial") {
    return (
      <span className="rounded-full border border-amber-400/30 bg-amber-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
        Partial
      </span>
    );
  }
  return (
    <span className="rounded-full border border-white/15 bg-white/[0.04] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-white/45">
      Unlabelled
    </span>
  );
}

/* ---- Elapsed / run metadata --------------------------------------------- */

export function RunMeta({ data, elapsedMs }: { data: RawResponse; elapsedMs: number | null }) {
  const runId = pickString(data, "run_id");
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10px] text-white/35">
      <span>
        elapsed <Value value={elapsedMs} suffix="ms" />
      </span>
      <span>
        run <Value value={runId} />
      </span>
    </div>
  );
}

/* ---- Designed empty / error states --------------------------------------- */

export function PanelEmptyState({
  status,
  error,
  onRunSingle,
  accentClass = "text-white/60",
}: {
  status: PanelStatus;
  error: string | null;
  onRunSingle?: () => void;
  accentClass?: string;
}) {
  if (status === "error") {
    return (
      <div className="mt-6 rounded-xl border border-rose-400/25 bg-rose-500/[0.05] p-6">
        <div className="text-sm font-semibold text-rose-200">This module did not complete</div>
        <p className="mt-1 font-mono text-xs text-rose-300/80">{error ?? "Unknown error"}</p>
        <p className="mt-2 text-xs text-white/45">
          No values are shown because none were returned. Check that the backend is reachable, then
          run this module again.
        </p>
        {onRunSingle && (
          <button
            onClick={onRunSingle}
            className="mt-4 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-1.5 text-xs font-medium text-rose-200 transition-colors hover:bg-rose-500/20"
          >
            Retry module
          </button>
        )}
      </div>
    );
  }

  if (status === "executing") {
    return (
      <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-6">
        <div className={`text-sm font-medium ${accentClass}`}>Executing…</div>
        <p className="mt-1 text-xs text-white/40">
          Values appear here when the backend responds. Nothing is shown before then.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-6 rounded-xl border border-dashed border-white/12 bg-black/20 p-6">
      <div className="text-sm font-medium text-white/60">No run yet</div>
      <p className="mt-1 text-xs text-white/40">
        This panel stays empty until a run returns real data. It does not display sample or
        placeholder figures.
      </p>
      {onRunSingle && (
        <button
          onClick={onRunSingle}
          className="mt-4 rounded-lg border border-white/15 bg-white/[0.04] px-3 py-1.5 text-xs font-medium text-white/70 transition-colors hover:bg-white/[0.08]"
        >
          Run this module
        </button>
      )}
    </div>
  );
}
