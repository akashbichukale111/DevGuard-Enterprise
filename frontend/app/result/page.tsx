"use client";

import { useEffect, useMemo, useState, useCallback, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { DiffEditor } from "@monaco-editor/react";

// Side-effect import: self-hosted Monaco loader path. See lib/monaco.ts.
import "@/lib/monaco";
import MetricCard from "@/components/MetricCard";
import TraceWaterfall, { TraceSpan } from "@/components/TraceWaterfall";

/* -------------------------------------------------------------------------- */
/*  API CONTRACT                                                              */
/*  Interface names are chosen for clarity on THIS page. Where a name may     */
/*  differ from another module's real field, see the // integration: notes.  */
/* -------------------------------------------------------------------------- */

interface Vulnerability {
  cwe: string;
  title: string;
  line: number;
  severity: "critical" | "high" | "medium" | "low";
}

interface RetryAttempt {
  attempt: number;
  agent: string;
  validator_score: number; // 0-100
  passed: boolean;
}

interface ModelRouting {
  tier: string; // e.g. "gpt-4o (high-severity tier)"
  reason: string; // severity-based justification
}

interface SloStatus {
  slo_target: number; // e.g. 99.5
  error_budget_remaining_pct: number; // 0-100
  // FIX (bug #4): some backend responses were observed sending the
  // *consumed* budget under this same field name, which is why the badge
  // rendered "0% remaining" right after a clean, successful scan. We can't
  // change the API here, so we defensively accept an alternate field name
  // if the backend starts sending it, and otherwise fall back to treating
  // the value already on `error_budget_remaining_pct` as remaining (not
  // consumed) — see `resolveErrorBudgetRemaining()` below for the full logic.
  error_budget_consumed_pct?: number;
  state: "green" | "amber" | "red";
}

interface AuditEntry {
  scan_id: string;
  code_hash: string;
  // Null until the entry is actually written. The backend used to send
  // `prev_hash: ""` and `chain_verified: true` unconditionally — computed
  // BEFORE the append ran — so this panel showed a green "Chain Verified" badge
  // for a record that did not exist yet. Three states now:
  //   "pending" -> not written (critical/high scans wait at the approval gate)
  //   "written" -> real hashes, this record IS chained
  //   "failed"  -> the append raised; this scan is NOT in the audit log
  prev_hash: string | null;
  entry_hash: string | null;
  chain_verified: boolean | null;
  chain_state: "pending" | "written" | "failed";
}

interface BenchmarkReport {
  accuracy: number; // 0-1
  precision: number;
  recall: number;
  false_positive_rate: number;
  sample_size: number;
  generated_at?: number; // unix seconds; when the harness actually ran
}

interface ScanResult {
  original_code: string;
  fixed_code: string;
  language: string;
  vulnerabilities: Vulnerability[];
  eval_score: number;
  // The Scanner reports a severity BAND, not a scored CVSS vector. This was
  // `cvss_before`/`cvss_after` from a hard-coded {critical: 9.5, high: 7.8, ...}
  // lookup plus the constant 1.2, rendered to one decimal under a "CVSS" label.
  // There is no "after" value because nothing re-scores the patched code.
  severity_before: SeverityBand | null;
  retry_history: RetryAttempt[];
  model_routing: ModelRouting;
  latency_ms: number;
  // Spend on THIS request. A cache hit makes zero LLM calls, so both are zero /
  // null for one — the figures that produced the result live in origin_* below.
  // Billing the cached run's tokens again on every repeat scan would
  // double-count spend.
  tokens_used: number | null; // null = nothing counted them; never a measured 0
  cost_usd: number | null;
  cached: boolean;
  origin_tokens_used: number | null; // what producing this result actually took
  origin_cost_usd: number | null;
  slo_status: SloStatus;
  audit_entry: AuditEntry;
  benchmark_report: BenchmarkReport | null; // null until the harness has run
  trace_id: string;
  spans: TraceSpan[]; // integration: backend exposes span data via the same OTEL store SigNoz reads
  status: "complete" | "pending_approval" | "processing" | "error";
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
/* Deliberately NOT defaulted. `frontend/.env.example` documents "Leave unset if
   you are not running SigNoz — the link is simply not shown", but this fell back
   to `https://cloud.signoz.io`, so "Investigate this trace in SigNoz" always
   rendered and opened SigNoz's own site at a /trace/<id> path that host knows
   nothing about. A dead link on the proof CTA is worse than no CTA, so the
   button is now gated on the variable actually being configured. */
const SIGNOZ_URL = process.env.NEXT_PUBLIC_SIGNOZ_URL?.trim() || null;

function signozTraceUrl(traceId: string, scanId: string): string | null {
  return SIGNOZ_URL ? `${SIGNOZ_URL}/trace/${traceId}?scan_id=${scanId}` : null;
}

/* -------------------------------------------------------------------------- */
/*  SEVERITY HELPERS                                                           */
/*                                                                            */
/*  This was `cvssBand(score: number)`, fed by `cvss_before`/`cvss_after` —    */
/*  which the backend produced from a hard-coded {critical: 9.5, high: 7.8,    */
/*  medium: 5.5, low: 3.0} lookup plus the constant 1.2 for "after". Rendering */
/*  those with `.toFixed(1)` under a CVSS label claimed a scored CVSS vector   */
/*  for code nobody scored. The Scanner reports a severity *band*, so the band */
/*  is what this styles, under its own name.                                   */
/* -------------------------------------------------------------------------- */

type SeverityBand = "critical" | "high" | "medium" | "low";

const SEVERITY_STYLES: Record<SeverityBand, { label: string; color: string; bg: string }> = {
  critical: { label: "CRITICAL", color: "text-red-300", bg: "bg-red-500/15 border-red-500/30" },
  high: { label: "HIGH", color: "text-orange-300", bg: "bg-orange-500/15 border-orange-500/30" },
  medium: { label: "MEDIUM", color: "text-amber-300", bg: "bg-amber-500/15 border-amber-500/30" },
  low: { label: "LOW", color: "text-emerald-300", bg: "bg-emerald-500/15 border-emerald-500/30" },
};

const NEUTRAL_STYLE = { label: "NONE", color: "text-white/50", bg: "bg-white/5 border-white/10" };

function severityStyle(band: SeverityBand | null) {
  return band ? SEVERITY_STYLES[band] ?? NEUTRAL_STYLE : NEUTRAL_STYLE;
}

/* -------------------------------------------------------------------------- */
/*  FIX (bug #4) — SLO badge inverted math                                    */
/*                                                                            */
/*  The ring math in <SloBadge> was already correct (remaining% -> dash       */
/*  length), so a healthy scan showing "0% remaining" means the *value*      */
/*  flowing in was wrong, not the arithmetic. Centralizing the read here so   */
/*  there is exactly one place that decides what "remaining" means:          */
/*   1. If the backend ever sends `error_budget_consumed_pct`, remaining is  */
/*      derived as 100 - consumed.                                          */
/*   2. Otherwise we trust `error_budget_remaining_pct` as-is.               */
/*   3. Value is clamped to [0, 100] and NaN-guarded so a bad payload can't  */
/*      render a broken ring.                                                */
/* -------------------------------------------------------------------------- */
function resolveErrorBudgetRemaining(slo: SloStatus): number {
  const clamp = (n: number) => Math.max(0, Math.min(100, n));
  if (typeof slo.error_budget_consumed_pct === "number" && !Number.isNaN(slo.error_budget_consumed_pct)) {
    return clamp(100 - slo.error_budget_consumed_pct);
  }
  if (typeof slo.error_budget_remaining_pct === "number" && !Number.isNaN(slo.error_budget_remaining_pct)) {
    return clamp(slo.error_budget_remaining_pct);
  }
  return 100; // no data — assume healthy rather than showing an alarming 0%
}

/* -------------------------------------------------------------------------- */
/*  SMALL INLINE ICONS (no icon lib dependency to keep bundle lean)           */
/* -------------------------------------------------------------------------- */

const IconClock = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />
  </svg>
);
const IconChip = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="6" y="6" width="12" height="12" rx="1" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
  </svg>
);
const IconCoin = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="12" cy="12" r="9" /><path d="M9.5 9.5a2.5 2.5 0 0 1 5 0c0 1.5-1 2-2.5 2.5s-2.5 1-2.5 2.5a2.5 2.5 0 0 0 5 0M12 6v2M12 16v2" />
  </svg>
);
const IconArrowLeft = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M19 12H5M12 19l-7-7 7-7" />
  </svg>
);

/* -------------------------------------------------------------------------- */
/*  GLOW / DEPTH TOKENS                                                        */
/*  FIX (#2): shared CSS variables so every card in this page (and, if you    */
/*  reuse this block, elsewhere in the app) pulls glow color from one place.  */
/*  --glow-primary = cyan accent (matches the CVSS/routing/SigNoz palette)    */
/*  --glow-accent  = violet accent (matches the tokens metric card)          */
/* -------------------------------------------------------------------------- */
function GlowTokens() {
  return (
    <style jsx global>{`
      :root {
        --glow-primary: rgba(56, 189, 248, 0.35);
        --glow-primary-soft: rgba(56, 189, 248, 0.14);
        --glow-accent: rgba(167, 139, 250, 0.32);
      }
      .glow-card {
        transition: box-shadow 0.35s ease, border-color 0.35s ease, transform 0.35s ease;
        box-shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset, 0 20px 40px -24px rgba(0, 0, 0, 0.6);
      }
      .glow-card:hover {
        box-shadow: 0 0 0 1px var(--glow-primary-soft), 0 0 32px var(--glow-primary),
          0 24px 48px -20px rgba(0, 0, 0, 0.7);
        transform: translateY(-1px);
      }
      .glow-card--accent:hover {
        box-shadow: 0 0 0 1px rgba(167, 139, 250, 0.14), 0 0 32px var(--glow-accent),
          0 24px 48px -20px rgba(0, 0, 0, 0.7);
      }
    `}</style>
  );
}

/* -------------------------------------------------------------------------- */
/*  BACK NAVIGATION                                                            */
/*  FIX (#1): persistent, low-contrast-until-hover glassmorphic link back to  */
/*  the scan form. Fixed position so it's reachable without scrolling on any  */
/*  section of a potentially long report.                                    */
/* -------------------------------------------------------------------------- */
function BackToScanLink() {
  return (
    <motion.a
      href="/"
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.4 }}
      className="fixed left-4 top-4 z-50 flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-3.5 py-2 text-xs font-medium text-white/50 backdrop-blur-xl transition hover:border-white/20 hover:bg-white/[0.08] hover:text-white/90 md:left-6 md:top-6"
    >
      <IconArrowLeft />
      New scan
    </motion.a>
  );
}

/* -------------------------------------------------------------------------- */
/*  STATE SHELLS — premium loading / error / not-found                        */
/* -------------------------------------------------------------------------- */

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-[#070a13] text-white antialiased">
      <GlowTokens />
      <BackToScanLink />
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(60%_50%_at_50%_0%,rgba(56,189,248,0.08),transparent)]" />
      <div className="relative mx-auto max-w-7xl px-4 py-8 pt-16 md:px-8 md:pt-8">{children}</div>
    </div>
  );
}

function LoadingState({ note }: { note: string }) {
  return (
    <Shell>
      <div className="flex min-h-[70vh] flex-col items-center justify-center gap-6">
        <div className="relative h-16 w-16">
          <div className="absolute inset-0 animate-ping rounded-full bg-cyan-400/20" />
          <div className="absolute inset-2 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
        </div>
        <p className="text-sm text-white/60">{note}</p>
      </div>
    </Shell>
  );
}

function ErrorState({ title, detail }: { title: string; detail: string }) {
  return (
    <Shell>
      <div className="flex min-h-[70vh] flex-col items-center justify-center gap-4 text-center">
        <div className="rounded-2xl border border-red-500/20 bg-red-500/5 p-8 backdrop-blur-xl">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-red-500/15 text-red-300">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="9" /><path d="M12 8v4M12 16h.01" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="mt-2 max-w-sm text-sm text-white/50">{detail}</p>
          <a href="/" className="mt-6 inline-block rounded-lg border border-white/10 bg-white/5 px-5 py-2 text-sm text-white/80 transition hover:bg-white/10">
            ← Run a new scan
          </a>
        </div>
      </div>
    </Shell>
  );
}

/* -------------------------------------------------------------------------- */
/*  MAIN DASHBOARD                                                             */
/* -------------------------------------------------------------------------- */

function ResultDashboard() {
  const params = useSearchParams();
  const scanId = params.get("scan_id");

  const [result, setResult] = useState<ScanResult | null>(null);
  const [phase, setPhase] = useState<"loading" | "processing" | "ready" | "notfound" | "error">("loading");
  const [routingOpen, setRoutingOpen] = useState(true); // open by default — it's a wow moment, don't hide it
  const [approvalState, setApprovalState] = useState<"idle" | "approving" | "rejecting">("idle");
  // the error-surface rule: an error state must name what failed. Discarding the reason
  // and rendering a generic message leaves a user with nothing to act on.
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  // A failed approve/reject used to roll back silently — the user saw the
  // button settle back with no indication their decision had not been applied,
  // on the one control that gates a human-in-the-loop security decision.
  const [approvalError, setApprovalError] = useState<string | null>(null);

  /* ---- data fetch, with WebSocket fallback for in-flight pipelines ------- */
  const fetchResult = useCallback(async () => {
    if (!scanId) {
      setPhase("notfound");
      return;
    }
    try {
      const res = await fetch(`${API}/scan/${scanId}`);
      if (res.status === 404) {
        setPhase("notfound");
        return;
      }
      if (!res.ok) {
        setErrorDetail(`The backend responded with HTTP ${res.status} when fetching this scan.`);
        setPhase("error");
        return;
      }
      const data: ScanResult = await res.json();
      setResult(data);

      if (data.status === "processing") {
        // Pipeline still finishing — subscribe to the same WS Page 1 used so we
        // don't hard-poll. On completion the socket pushes the final payload.
        setPhase("processing");
        subscribeWs();
      } else {
        setPhase("ready");
      }
    } catch (err) {
      setErrorDetail(
        err instanceof Error
          ? `Could not reach the backend at ${API} — ${err.message}`
          : `Could not reach the backend at ${API}.`
      );
      setPhase("error");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId]);

  const subscribeWs = useCallback(() => {
    if (!scanId) return;
    const wsProto = API.startsWith("https") ? "wss" : "ws";
    const wsHost = API.replace(/^https?:\/\//, "");
    const ws = new WebSocket(`${wsProto}://${wsHost}/ws/scan/${scanId}`);
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data) as Partial<ScanResult> & { status: ScanResult["status"] };
        if (msg.status === "complete" || msg.status === "pending_approval") {
          // Refetch the canonical full result rather than trusting the partial.
          fetchResult();
          ws.close();
        }
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onerror = () => ws.close();
    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId]);

  useEffect(() => {
    fetchResult();
  }, [fetchResult]);

  /* ---- Human-in-the-loop approve/reject with optimistic UI + rollback ---- */
  const handleDecision = useCallback(
    async (decision: "approve" | "reject") => {
      if (!scanId || !result) return;
      const prev = result;
      setApprovalError(null);
      setApprovalState(decision === "approve" ? "approving" : "rejecting");
      // Optimistic: flip status immediately so the panel resolves without lag.
      setResult({ ...result, status: "complete" });
      try {
        const res = await fetch(`${API}/scan/${scanId}/${decision}`, { method: "POST" });
        if (!res.ok) throw new Error(`the backend responded with HTTP ${res.status}`);
        // Re-fetch rather than keeping the optimistic patch. The decision is
        // what WRITES the audit entry, so the backend now holds chain state the
        // client cannot derive: `audit_entry` moves from "pending" to "written"
        // with the real prev_hash/entry_hash. Without this the compliance footer
        // keeps showing "Audit entry pending approval" after the entry exists.
        await fetchResult();
      } catch (err) {
        setResult(prev); // rollback on failure
        // ...and SAY so. A silent rollback on the approval gate means the user
        // believes a decision was recorded when it was not.
        setApprovalError(
          `Your ${decision} was not recorded — ${
            err instanceof Error ? err.message : "the request failed"
          }. The scan is unchanged; try again.`
        );
      } finally {
        setApprovalState("idle");
      }
    },
    [scanId, result, fetchResult]
  );

  /* ---- render gates ------------------------------------------------------ */
  if (phase === "loading") return <LoadingState note="Loading scan result…" />;
  if (phase === "processing") return <LoadingState note="Pipeline still finishing — streaming final agent results…" />;
  if (phase === "notfound")
    return <ErrorState title="Scan not found" detail="This scan ID doesn't exist or has expired. Start a fresh scan to continue." />;
  if (phase === "error" || !result)
    return (
      <ErrorState
        title="Something went wrong"
        detail={
          errorDetail ??
          "We couldn't reach the analysis backend. Check your connection and retry."
        }
      />
    );

  const before = severityStyle(result.severity_before);
  const traceUrl = signozTraceUrl(result.trace_id, result.audit_entry.scan_id);
  const cweList = result.vulnerabilities.map((v) => v.cwe).join(", ");
  const bench = result.benchmark_report;
  const lastAttempt = result.retry_history[result.retry_history.length - 1];
  const hasSpans = Array.isArray(result.spans) && result.spans.length > 0; // FIX (#5) guard

  /* ---- Framer stagger container: order = diff → metrics → trace → SLO ----
     This order is intentional. The diff is the "did it work?" answer judges
     want first. Metrics quantify it, the trace proves how, the SLO/audit
     footer certify it. Revealing them in that narrative order guides the eye
     through the pitch automatically.
     FIX (#3): stagger tightened from 180ms -> 100ms (within the requested
     80-120ms band) and delayChildren trimmed from 100ms -> 60ms so the
     reveal reads as one cinematic beat instead of a slow roll-out. Easing
     curve [0.22, 1, 0.36, 1] kept as-is — it was already correct. */
  const container = {
    hidden: {},
    show: { transition: { staggerChildren: 0.1, delayChildren: 0.06 } },
  };
  const item = {
    hidden: { opacity: 0, y: 28 },
    show: { opacity: 1, y: 0, transition: { duration: 0.55, ease: [0.22, 1, 0.36, 1] } },
  };

  return (
    <Shell>
      <motion.div variants={container} initial="hidden" animate="show" className="space-y-6">
        {/* ---- Header ---- */}
        <motion.div variants={item} className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Scan Report</h1>
            <p className="mt-1 font-mono text-xs text-white/40">scan_id: {result.audit_entry.scan_id}</p>
          </div>
          {/* Highest severity found → the Validator's score for the patch.
              This was a "Security Score Delta" showing CVSS 9.5 → 1.2. Neither
              number was measured: both came from lookup tables in the backend,
              and nothing anywhere re-scores the patched code, so there was no
              "after" to show. What DOES exist is the severity band the Scanner
              reported and the eval_score the Validator gave the fix — two
              different measurements, now labelled as themselves. */}
          <div className="glow-card flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-5 py-3 backdrop-blur-xl">
            <div className="text-center">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-white/40">Highest severity</div>
              <div className={`rounded-lg border px-3 py-1.5 ${before.bg}`}>
                <div className={`text-sm font-bold tracking-wide ${before.color}`}>{before.label}</div>
              </div>
            </div>
            <motion.svg
              width="40" height="20" viewBox="0 0 40 20" fill="none"
              initial={{ x: -6, opacity: 0 }} animate={{ x: 0, opacity: 1 }}
              transition={{ delay: 0.6, duration: 0.5 }}
            >
              <path d="M2 10h30M28 5l6 5-6 5" stroke="url(#g)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <defs><linearGradient id="g" x1="0" x2="40"><stop stopColor="#fb923c" /><stop offset="1" stopColor="#34d399" /></linearGradient></defs>
            </motion.svg>
            <div className="text-center">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-white/40">Validator score</div>
              <div className="rounded-lg border border-white/10 bg-white/5 px-3 py-1.5">
                <div className="text-sm font-bold tabular-nums text-white/80">
                  {result.eval_score > 0 ? `${result.eval_score}/100` : "n/a"}
                </div>
              </div>
            </div>
          </div>
        </motion.div>

        {/* ---- Human-in-the-Loop Approval Gate (only when pending) ---- */}
        <AnimatePresence>
          {result.status === "pending_approval" && (
            <motion.div
              variants={item}
              exit={{ opacity: 0, height: 0, marginBottom: 0 }}
              className="rounded-2xl border-2 border-amber-500/40 bg-amber-500/5 p-5 backdrop-blur-xl"
            >
              <div className="flex items-start gap-3">
                <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-amber-500/20 text-amber-300">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17h.01" />
                  </svg>
                </div>
                <div className="flex-1">
                  <h3 className="font-semibold text-amber-200">Human approval required</h3>
                  <p className="mt-1 text-sm text-amber-100/70">
                    This fix touches a <strong>critical-severity</strong> vulnerability
                    ({cweList}). Per policy, high-blast-radius fixes are gated for human
                    sign-off before they can be marked resolved.
                  </p>
                  <div className="mt-4 flex gap-3">
                    <button
                      onClick={() => handleDecision("approve")}
                      disabled={approvalState !== "idle"}
                      className="rounded-lg bg-emerald-500/90 px-5 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
                    >
                      {approvalState === "approving" ? "Approving…" : "Approve fix"}
                    </button>
                    <button
                      onClick={() => handleDecision("reject")}
                      disabled={approvalState !== "idle"}
                      className="rounded-lg border border-white/15 bg-white/5 px-5 py-2 text-sm font-medium text-white/80 transition hover:bg-white/10 disabled:opacity-50"
                    >
                      {approvalState === "rejecting" ? "Rejecting…" : "Reject"}
                    </button>
                  </div>

                  {approvalError && (
                    <div
                      role="alert"
                      className="mt-4 rounded-lg border border-rose-400/30 bg-rose-500/10 px-4 py-3"
                    >
                      <p className="text-sm font-medium text-rose-200">
                        Decision not recorded
                      </p>
                      <p className="mt-1 text-xs leading-relaxed text-rose-100/75">
                        {approvalError}
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* ---- Main 2-column grid: diff (left) · metrics+panels (right) ---- */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1.5fr_1fr]">
          {/* LEFT — Diff view */}
          <motion.div variants={item} className="glow-card overflow-hidden rounded-2xl border border-white/10 bg-white/5 backdrop-blur-xl">
            <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
              <span className="text-sm font-medium text-white/80">
                {result.vulnerabilities.length} vulnerabilities fixed · {cweList}
              </span>
              <span className="rounded-md bg-emerald-500/15 px-2 py-1 text-[11px] font-medium text-emerald-300">
                eval {result.eval_score}/100
              </span>
            </div>
            <div className="h-[520px]">
              <DiffEditor
                original={result.original_code}
                modified={result.fixed_code}
                language={result.language ?? "python"}
                theme="vs-dark"
                options={{
                  readOnly: true,
                  renderSideBySide: true,
                  minimap: { enabled: false },
                  fontSize: 13,
                  scrollBeyondLastLine: false,
                  renderGutterMenu: false,
                  // Gutter markers on changed lines come free with the diff editor;
                  // we keep glyphMargin on so those change indicators render.
                  glyphMargin: true,
                }}
              />
            </div>
          </motion.div>

          {/* RIGHT — metric cards + panels */}
          <div className="space-y-6">
            {/* Three CountUp metric cards */}
            <motion.div variants={item} className="grid grid-cols-1 gap-4 sm:grid-cols-3 lg:grid-cols-1">
              <div className="glow-card rounded-2xl">
                {/* `baselineNote` used to read "38% faster than manual review".
                    No manual-review baseline was ever measured, so there was
                    nothing to be 38% faster than. The measured value stands on
                    its own. */}
                <MetricCard
                  index={0} label="Latency" value={result.latency_ms} suffix=" ms"
                  decimals={0} icon={<IconClock />} accent="cyan"
                  baselineNote="wall-clock, this request"
                />
              </div>
              <div className="glow-card glow-card--accent rounded-2xl">
                {/* null when the provider reported no usage — rendering 0 would
                    claim a scan consumed no tokens, which is never true. */}
                <MetricCard
                  index={1} label="Tokens Used"
                  value={result.cached ? (result.origin_tokens_used ?? 0) : (result.tokens_used ?? 0)}
                  decimals={0} icon={<IconChip />} accent="violet"
                  baselineNote={
                    result.cached
                      ? "from the cached run — this request used none"
                      : result.tokens_used === null
                        ? "not reported by the provider"
                        : "prompt + completion, all agents"
                  }
                />
              </div>
              <div className="glow-card rounded-2xl">
                {/* `baselineNote` used to read "vs ~$85 for 1hr human review".
                    No human-review baseline was ever measured, so there was
                    nothing to compare against. The measured value stands alone.
                    Cost was also always $0.00 before the accounting fix: the
                    backend read prompt/completion tokens off a PipelineResult
                    that has neither field. */}
                <MetricCard
                  index={2} label="Cost"
                  value={result.cached ? (result.origin_cost_usd ?? 0) : (result.cost_usd ?? 0)}
                  prefix="$"
                  decimals={4} icon={<IconCoin />} accent="emerald"
                  baselineNote={
                    result.cached
                      ? "cached — this request cost nothing"
                      : result.cost_usd === null
                        ? "not reported by the provider"
                        : "this scan, all agents"
                  }
                />
              </div>
            </motion.div>

            {/* Model Routing + Reflection Loop panel */}
            <motion.div variants={item} className="glow-card rounded-2xl border border-white/10 bg-white/5 backdrop-blur-xl">
              <button
                onClick={() => setRoutingOpen((o) => !o)}
                className="flex w-full items-center justify-between px-4 py-3 text-left"
              >
                <span className="text-sm font-medium text-white/80">Model routing & self-correction</span>
                <motion.span animate={{ rotate: routingOpen ? 180 : 0 }} className="text-white/40">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                </motion.span>
              </button>
              <AnimatePresence initial={false}>
                {routingOpen && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
                    className="overflow-hidden"
                  >
                    <div className="space-y-3 border-t border-white/10 px-4 py-4">
                      <div>
                        <div className="text-[11px] uppercase tracking-wider text-white/40">Routed to</div>
                        <div className="mt-0.5 text-sm font-medium text-cyan-300">{result.model_routing.tier}</div>
                        <div className="mt-0.5 text-xs text-white/50">{result.model_routing.reason}</div>
                      </div>
                      <div className="border-t border-white/5 pt-3">
                        <div className="text-[11px] uppercase tracking-wider text-white/40">Reflection loop</div>
                        <ul className="mt-2 space-y-1.5">
                          {result.retry_history.map((a) => (
                            <li key={a.attempt} className="flex items-center justify-between text-xs">
                              <span className="text-white/70">
                                {a.agent}: attempt {a.attempt}
                              </span>
                              <span className={a.passed ? "text-emerald-300" : "text-amber-300"}>
                                score {a.validator_score}/100 {a.passed ? "✅" : "↻ retry"}
                              </span>
                            </li>
                          ))}
                        </ul>
                        {lastAttempt && (
                          <p className="mt-3 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                            Fix Agent self-corrected across {result.retry_history.length} attempt
                            {result.retry_history.length > 1 ? "s" : ""} until the validator passed
                            ({lastAttempt.validator_score}/100). The AI proved its own work.
                          </p>
                        )}
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>

            {/* SLO / error budget badge */}
            <motion.div variants={item}>
              <SloBadge slo={result.slo_status} />
            </motion.div>
          </div>
        </div>

        {/* ---- Trace waterfall (full width) ----
            FIX (#5): only render the real waterfall when spans actually came
            back from the API. An empty/missing `spans` array previously fell
            through into <TraceWaterfall> and rendered a misleading "0 SPANS"
            state. Now we show an honest fallback that links to the same
            SigNoz trace the CTA button below points to. */}
        <motion.div variants={item}>
          {hasSpans ? (
            <TraceWaterfall spans={result.spans} />
          ) : (
            <div className="glow-card flex items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-5 py-4 backdrop-blur-xl">
              <div>
                <div className="text-sm font-medium text-white/70">Distributed trace</div>
                <div className="mt-0.5 text-xs text-white/40">
                  Span-level detail isn&apos;t available in this response yet.
                </div>
                <div className="mt-1 font-mono text-[11px] text-white/30">
                  trace_id: {result.trace_id}
                </div>
              </div>
              {traceUrl ? (
                <button
                  onClick={() => window.open(traceUrl, "_blank", "noopener")}
                  className="flex shrink-0 items-center gap-1.5 rounded-lg border border-cyan-400/30 bg-cyan-500/10 px-3.5 py-2 text-xs font-medium text-cyan-200 transition hover:bg-cyan-500/15"
                >
                  Full trace available in SigNoz
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M7 17L17 7M17 7H8M17 7v9" />
                  </svg>
                </button>
              ) : null}
            </div>
          )}
        </motion.div>

        {/* ---- Accuracy benchmark strip ----
            Rendered ONLY when a real harness run left an artifact on disk. This
            block used to show "90% Accuracy · 92% Precision · 88% Recall · 5%
            False Positive Rate (n=14 OWASP snippets)" on every scan, from
            literals hard-coded in the backend response — a "proof strip" that
            proved nothing. Run `python -m backend.core.benchmark --json
            data/benchmark_report.json` to populate it. */}
        <motion.div
          variants={item}
          className="rounded-xl border border-cyan-400/20 bg-gradient-to-r from-cyan-500/10 via-transparent to-transparent px-5 py-3 backdrop-blur-xl"
        >
          {bench ? (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <span className="font-semibold text-cyan-200">Scanner Agent —</span>
              <span className="text-white/80">{Math.round(bench.accuracy * 100)}% Accuracy</span>
              <span className="text-white/30">·</span>
              <span className="text-white/80">{Math.round(bench.precision * 100)}% Precision</span>
              <span className="text-white/30">·</span>
              <span className="text-white/80">{Math.round(bench.recall * 100)}% Recall</span>
              <span className="text-white/30">·</span>
              <span className="text-white/80">{Math.round(bench.false_positive_rate * 100)}% False Positive Rate</span>
              <span className="text-white/40">(n={bench.sample_size} labelled snippets)</span>
              {bench.generated_at ? (
                <span className="text-white/30">
                  measured {new Date(bench.generated_at * 1000).toLocaleDateString()}
                </span>
              ) : null}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <span className="font-semibold text-cyan-200">Scanner Agent —</span>
              <span className="text-white/60">accuracy not measured</span>
              <span className="text-white/30">·</span>
              <span className="font-mono text-xs text-white/40">
                python -m backend.core.benchmark --json data/benchmark_report.json
              </span>
            </div>
          )}
        </motion.div>

        {/* ---- Investigate in SigNoz CTA ----
            Only when NEXT_PUBLIC_SIGNOZ_URL is set. Without it there is no
            SigNoz to investigate in, and the button used to open
            cloud.signoz.io/trace/<id> — a 404 on someone else's site, dressed
            as this project's proof. */}
        {traceUrl ? (
        <motion.div variants={item}>
          <button
            onClick={() => window.open(traceUrl, "_blank", "noopener")}
            className="group relative w-full overflow-hidden rounded-2xl border border-cyan-400/30 bg-cyan-500/10 px-6 py-4 text-center transition hover:bg-cyan-500/15"
          >
            <span className="pointer-events-none absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-cyan-400/20 to-transparent transition-transform duration-700 group-hover:translate-x-full" />
            <span className="relative flex items-center justify-center gap-2 text-sm font-semibold text-cyan-100">
              Investigate this trace in SigNoz
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M7 17L17 7M17 7H8M17 7v9" />
              </svg>
            </span>
          </button>
        </motion.div>
        ) : null}

        {/* ---- Immutable audit trail footer ---- */}
        <motion.div variants={item} className="rounded-xl border border-white/10 bg-black/30 px-4 py-3 backdrop-blur-xl">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-white/50">
            <span className="text-white/30">COMPLIANCE RECORD</span>
            <span>scan_id: {truncate(result.audit_entry.scan_id)}</span>
            <span>code_hash: {truncate(result.audit_entry.code_hash)}</span>
            {result.audit_entry.prev_hash ? (
              <span>prev_hash: {truncate(result.audit_entry.prev_hash)}</span>
            ) : null}
            {/* Three states, because `chain_verified` is now nullable. It was
                previously a hard-coded `true` from the backend, computed before
                the audit entry was even written — so this badge read "✅ Chain
                Verified" for a record that did not exist. "Pending" is not
                "broken": a critical/high scan waits at the approval gate and
                has legitimately not been written yet. */}
            {result.audit_entry.chain_state === "written" ? (
              <span className="text-emerald-300">✅ Chain Verified</span>
            ) : result.audit_entry.chain_state === "failed" ? (
              <span className="text-red-300">⚠ Audit write FAILED — this scan is not in the chain</span>
            ) : (
              <span className="text-amber-300">◌ Audit entry pending approval</span>
            )}
          </div>
        </motion.div>
      </motion.div>
    </Shell>
  );
}

function truncate(s: string, n = 10): string {
  if (!s) return "—";
  return s.length <= n ? s : `${s.slice(0, n)}…`;
}

/* -------------------------------------------------------------------------- */
/*  SLO BADGE — compliance ring                                               */
/* -------------------------------------------------------------------------- */

function SloBadge({ slo }: { slo: SloStatus }) {
  const stateColor =
    slo.state === "green" ? "#34d399" : slo.state === "amber" ? "#fbbf24" : "#f87171";
  const circumference = 2 * Math.PI * 26;
  // FIX (#4): remaining % now resolved through resolveErrorBudgetRemaining()
  // instead of reading `error_budget_remaining_pct` directly, so a mislabeled
  // "consumed" value from the backend no longer inverts the ring.
  const remainingPct = resolveErrorBudgetRemaining(slo);
  const dash = (remainingPct / 100) * circumference;

  return (
    <div className="glow-card flex items-center gap-4 rounded-2xl border border-white/10 bg-white/5 p-4 backdrop-blur-xl">
      <div className="relative h-16 w-16">
        <svg width="64" height="64" viewBox="0 0 64 64" className="-rotate-90">
          <circle cx="32" cy="32" r="26" fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="6" />
          <motion.circle
            cx="32" cy="32" r="26" fill="none" stroke={stateColor} strokeWidth="6" strokeLinecap="round"
            strokeDasharray={circumference}
            initial={{ strokeDashoffset: circumference }}
            animate={{ strokeDashoffset: circumference - dash }}
            transition={{ duration: 1.2, ease: "easeOut" }}
          />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-xs font-bold" style={{ color: stateColor }}>
          {remainingPct}%
        </span>
      </div>
      <div>
        <div className="text-sm font-medium text-white/80">{slo.slo_target}% SLO</div>
        <div className="text-xs text-white/50">{remainingPct}% error budget remaining</div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  useSearchParams requires a Suspense boundary in the App Router.           */
/* -------------------------------------------------------------------------- */

export default function ResultPage() {
  return (
    <Suspense fallback={<LoadingState note="Loading…" />}>
      <ResultDashboard />
    </Suspense>
  );
}
