// frontend/app/nexus/page.tsx
"use client";

import { useCallback, useMemo, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import CountUp from "react-countup";

// ── Real per-module HUD components ──────────────────────────────────────────
// Each of these lives alongside this file in frontend/app/nexus/. They render
// the module-specific readout (Omni-Heal diff view, FinOps budget bar,
// Pre-Cog forecast chart, LLM Judge confidence meter, Exec Commander digest)
// but are expected to inherit the same HUD panel chrome (corner brackets,
// accent glow, status pill) established by ModulePanel below.
import OmniHealPanel from "../../components/nexus/OmniHealPanel";
import FinOpsPanel from "../../components/nexus/FinOpsPanel";
import PreCogPanel from "../../components/nexus/PreCogPanel";
import LLMJudgePanel from "../../components/nexus/LLMJudgePanel";
import ExecutiveCommanderPanel from "../../components/nexus/ExecutiveCommanderPanel";

// ── Design language ──────────────────────────────────────────────────────────
// This page intentionally reuses Page 1's visual vocabulary (glass panels,
// indigo/cyan glow tokens, [0.22,1,0.36,1] easing, conic shimmer borders) so
// Workspace -> Result -> Nexus reads as one product, not three demos bolted
// together. What's NEW here is the "command center" layer on top: HUD corner
// brackets, a scanline overlay, and per-module status telemetry — because
// Nexus's job isn't editing code, it's *commanding five autonomous agents at
// once*, and the chrome should say that at a glance.

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type ModuleKey =
  | "omniHeal"
  | "finOps"
  | "preCog"
  | "truthSerum"
  | "execCommander";

type ModuleStatus = "idle" | "executing" | "complete" | "error";

interface ModuleResult {
  status: ModuleStatus;
  data: Record<string, unknown> | null;
  error: string | null;
  elapsedMs: number | null;
}

interface ModuleDef {
  key: ModuleKey;
  code: string; // HUD module designation, e.g. "MOD-01"
  title: string;
  subtitle: string;
  description: string;
  endpoint: string;
  accent: "cyan" | "violet" | "emerald" | "amber" | "rose";
  stat: { label: string; value: number; suffix?: string; prefix?: string; decimals?: number };
}

const MODULES: ModuleDef[] = [
  {
    key: "omniHeal",
    code: "MOD-01",
    title: "Omni-Heal",
    subtitle: "Autonomous Code Remediation",
    description:
      "Runs the full Scanner \u2192 Fixer \u2192 Validator reflection loop against live code and streams the resulting diff back in real time — the same self-healing pass that powers the core pipeline, exposed here as a single commandable unit.",
    endpoint: "/god-mode/simulate/error",
    accent: "cyan",
    stat: { label: "Max reflection retries", value: 3 },
  },
  {
    key: "finOps",
    code: "MOD-02",
    title: "FinOps Agent",
    subtitle: "Autonomous Cost Controller",
    description:
      "Reads live LLM spend trends and recommends OTel sampling-ratio adjustments before budget pressure forces a model downgrade — with a hard-coded floor that never touches critical-severity scans.",
    endpoint: "/god-mode/simulate/cost-spike",
    accent: "emerald",
    stat: { label: "30-min cost budget", value: 5, prefix: "$", decimals: 2 },
  },
  {
    key: "preCog",
    code: "MOD-03",
    title: "Pre-Cog Ops",
    subtitle: "Future-State Predictor",
    description:
      "Extrapolates current error-rate and memory drift forward across a rolling horizon to forecast circuit-breaker trips and OOM risk before they happen — auto-scaling recommendations, not just alerts.",
    endpoint: "/god-mode/simulate/memory-leak",
    accent: "violet",
    stat: { label: "Forecast horizon", value: 15, suffix: " min" },
  },
  {
    key: "truthSerum",
    code: "MOD-04",
    title: "Truth Serum Agent",
    subtitle: "LLM Hallucination Judge",
    description:
      "Cross-examines the Scanner Agent's own findings, flagging any low-confidence or fabricated vulnerability before it ever reaches the Fixer — a skeptic embedded inside the pipeline, not bolted on after it.",
    endpoint: "/god-mode/simulate/hallucination",
    accent: "amber",
    stat: { label: "Confidence floor", value: 0.5, decimals: 2 },
  },
  {
    key: "execCommander",
    code: "MOD-05",
    title: "Executive SRE Commander",
    subtitle: "Mobile Sync & Incident Digest",
    description:
      "Aggregates all four modules into one Slack-ready incident brief and a structured outline for PDF export — the report a human exec reads after the machines have already handled it.",
    endpoint: "/god-mode/simulate/god-mode",
    accent: "rose",
    stat: { label: "Modules aggregated", value: 4 },
  },
];

const ACCENTS: Record<
  ModuleDef["accent"],
  { text: string; ring: string; glowRgb: string; dot: string; border: string }
> = {
  cyan: {
    text: "text-cyan-300",
    ring: "ring-cyan-400/20",
    glowRgb: "34, 211, 238",
    dot: "bg-cyan-400",
    border: "border-cyan-400/25",
  },
  violet: {
    text: "text-violet-300",
    ring: "ring-violet-400/20",
    glowRgb: "167, 139, 250",
    dot: "bg-violet-400",
    border: "border-violet-400/25",
  },
  emerald: {
    text: "text-emerald-300",
    ring: "ring-emerald-400/20",
    glowRgb: "52, 211, 153",
    dot: "bg-emerald-400",
    border: "border-emerald-400/25",
  },
  amber: {
    text: "text-amber-300",
    ring: "ring-amber-400/20",
    glowRgb: "251, 191, 36",
    dot: "bg-amber-400",
    border: "border-amber-400/25",
  },
  rose: {
    text: "text-rose-300",
    ring: "ring-rose-400/20",
    glowRgb: "251, 113, 133",
    dot: "bg-rose-400",
    border: "border-rose-400/25",
  },
};

const STATUS_LABEL: Record<ModuleStatus, string> = {
  idle: "STANDBY",
  executing: "EXECUTING",
  complete: "COMPLETE",
  error: "FAULT",
};

const initialResults: Record<ModuleKey, ModuleResult> = MODULES.reduce(
  (acc, m) => {
    acc[m.key] = { status: "idle", data: null, error: null, elapsedMs: null };
    return acc;
  },
  {} as Record<ModuleKey, ModuleResult>
);

// ── Page ─────────────────────────────────────────────────────────────────────

export default function NexusCommandCenter() {
  const [isGodModeActive, setIsGodModeActive] = useState(false);
  const [results, setResults] = useState<Record<ModuleKey, ModuleResult>>(initialResults);

  const loadingStates = useMemo(
    () =>
      MODULES.reduce((acc, m) => {
        acc[m.key] = results[m.key].status === "executing";
        return acc;
      }, {} as Record<ModuleKey, boolean>),
    [results]
  );

  const completeCount = MODULES.filter((m) => results[m.key].status === "complete").length;
  const errorCount = MODULES.filter((m) => results[m.key].status === "error").length;

  const runModule = useCallback(async (mod: ModuleDef) => {
    const started = performance.now();
    setResults((prev) => ({
      ...prev,
      [mod.key]: { status: "executing", data: null, error: null, elapsedMs: null },
    }));

    try {
      const res = await fetch(`${API_BASE}${mod.endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const elapsedMs = Math.round(performance.now() - started);

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      setResults((prev) => ({
        ...prev,
        [mod.key]: { status: "complete", data, error: null, elapsedMs },
      }));
    } catch (err) {
      const elapsedMs = Math.round(performance.now() - started);
      setResults((prev) => ({
        ...prev,
        [mod.key]: {
          status: "error",
          data: null,
          error: err instanceof Error ? err.message : "Unreachable",
          elapsedMs,
        },
      }));
    }
  }, []);

  // POLISH: Promise.all fires all five module calls in the same tick — the
  // whole point of "God Mode" is that these agents run CONCURRENTLY, not as
  // a sequential checklist. Each module still tracks its own status
  // independently, so the UI shows five simultaneous live readouts rather
  // than one composite spinner.
  const handleInitiateGodMode = useCallback(async () => {
    if (isGodModeActive) return;
    setIsGodModeActive(true);
    try {
      await Promise.all(MODULES.map((mod) => runModule(mod)));
    } finally {
      setIsGodModeActive(false);
    }
  }, [isGodModeActive, runModule]);

  return (
    <>
      <GlobalStyles />
      <main className="relative min-h-screen w-full overflow-hidden bg-[#05060a] text-slate-200 antialiased">
        <div className="pointer-events-none absolute inset-0 dg-grid opacity-[0.35]" />
        <div className="pointer-events-none absolute -top-40 left-1/2 h-[700px] w-[1000px] -translate-x-1/2 rounded-full dg-bloom" />
        <div className="pointer-events-none absolute inset-0 dg-scanlines opacity-[0.5]" />

        <div className="relative z-10 mx-auto flex min-h-screen w-full max-w-6xl flex-col px-4 py-8 sm:px-6 lg:py-12 xl:max-w-7xl">
          <NexusHeader completeCount={completeCount} errorCount={errorCount} />

          <StatusStrip results={results} />

          {/* The primary action sits above the readouts, not below them. It
              used to render after all five panels, which on a 1600x1000 screen
              put it at y=2030 — two full viewports of scrolling before the
              main control of a command deck came into view. */}
          <GodModeButton
            onClick={handleInitiateGodMode}
            active={isGodModeActive}
            completeCount={completeCount}
          />

          {/* ── Module readouts ───────────────────────────────────────────────
              Each module now renders its own dedicated panel component instead
              of the generic <ModulePanel>. The generic component is kept below
              (unused) as a reference/fallback for the shared HUD chrome each
              of these five should replicate — corner brackets, accent glow,
              status pill, stat + readout column. */}
          <section className="mt-10 grid grid-cols-1 gap-6 xl:grid-cols-2">
            <OmniHealPanel
              data={results.omniHeal.data}
              status={results.omniHeal.status}
              error={results.omniHeal.error}
              elapsedMs={results.omniHeal.elapsedMs}
              onRunSingle={() => runModule(MODULES[0])}
            />
            <FinOpsPanel
              data={results.finOps.data}
              status={results.finOps.status}
              error={results.finOps.error}
              elapsedMs={results.finOps.elapsedMs}
              onRunSingle={() => runModule(MODULES[1])}
            />
            <PreCogPanel
              data={results.preCog.data}
              status={results.preCog.status}
              error={results.preCog.error}
              elapsedMs={results.preCog.elapsedMs}
              onRunSingle={() => runModule(MODULES[2])}
            />
            <LLMJudgePanel
              data={results.truthSerum.data}
              status={results.truthSerum.status}
              error={results.truthSerum.error}
              elapsedMs={results.truthSerum.elapsedMs}
              onRunSingle={() => runModule(MODULES[3])}
            />
            <div className="min-w-0 xl:col-span-2">
              <ExecutiveCommanderPanel
                data={results.execCommander.data}
                status={results.execCommander.status}
                error={results.execCommander.error}
                elapsedMs={results.execCommander.elapsedMs}
                onRunSingle={() => runModule(MODULES[4])}
              />
            </div>
          </section>

          <footer className="mt-10 flex items-center justify-center gap-4 pb-6 text-[11px] text-slate-600">
            <Link href="/" className="transition-colors hover:text-slate-400">
              &larr; Agent Workspace
            </Link>
            <span className="h-1 w-1 rounded-full bg-slate-700" />
            <span>SigNoz Nexus &middot; 5-module command layer</span>
          </footer>
        </div>
      </main>
    </>
  );
}

// ── Header ───────────────────────────────────────────────────────────────────

function NexusHeader({
  completeCount,
  errorCount,
}: {
  completeCount: number;
  errorCount: number;
}) {
  return (
    <header className="flex flex-col gap-6 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-center gap-3">
        <div className="dg-logo flex h-10 w-10 items-center justify-center rounded-lg text-base font-bold text-white">
          &#9737;
        </div>
        <div className="leading-tight">
          <h1 className="text-[17px] font-semibold tracking-tight text-white">
            SigNoz <span className="text-[color:var(--dg-accent)]">Nexus</span>
          </h1>
          <p className="text-[11px] uppercase tracking-[0.15em] text-slate-500">
            Command Center &middot; God Mode Interface
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 dg-ping" />
        <span className="text-[11px] font-medium text-slate-300">
          {completeCount}/5 modules reporting
        </span>
        {errorCount > 0 && (
          <span className="ml-1 text-[11px] font-medium text-rose-300">
            &middot; {errorCount} fault{errorCount > 1 ? "s" : ""}
          </span>
        )}
      </div>
    </header>
  );
}

// ── Status strip (5 dots, one per module) ───────────────────────────────────

function StatusStrip({ results }: { results: Record<ModuleKey, ModuleResult> }) {
  return (
    <div className="mt-8 grid grid-cols-2 gap-2 sm:grid-cols-5">
      {MODULES.map((mod) => {
        const r = results[mod.key];
        const a = ACCENTS[mod.accent];
        return (
          <div
            key={mod.key}
            className="flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2"
          >
            <span
              className={`h-1.5 w-1.5 flex-none rounded-full ${
                r.status === "executing"
                  ? `${a.dot} dg-ping`
                  : r.status === "complete"
                  ? "bg-emerald-400"
                  : r.status === "error"
                  ? "bg-rose-400"
                  : "bg-slate-700"
              }`}
            />
            <span className="truncate text-[10px] font-medium uppercase tracking-wider text-slate-500">
              {mod.code}
            </span>
            <span className="ml-auto text-[10px] font-semibold text-slate-400">
              {STATUS_LABEL[r.status]}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── Module panel (generic — no longer rendered directly above, kept as the
// shared HUD chrome reference for the 5 dedicated panel components) ─────────

function ModulePanel({
  mod,
  index,
  result,
  onRunSingle,
}: {
  mod: ModuleDef;
  index: number;
  result: ModuleResult;
  onRunSingle: () => void;
}) {
  const a = ACCENTS[mod.accent];
  const isExecuting = result.status === "executing";

  return (
    <motion.div
      initial={{ opacity: 0, y: 28 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
      className={`dg-hud-panel relative overflow-hidden rounded-2xl border ${a.border} bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ${a.ring} sm:p-8`}
      style={{ ["--panel-glow" as string]: a.glowRgb }}
    >
      {/* HUD corner brackets */}
      <span className="dg-corner dg-corner-tl" />
      <span className="dg-corner dg-corner-tr" />
      <span className="dg-corner dg-corner-bl" />
      <span className="dg-corner dg-corner-br" />

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start lg:justify-between">
        <div className="max-w-2xl">
          <div className="flex items-center gap-3">
            <span
              className={`rounded-md border ${a.border} px-2 py-0.5 text-[10px] font-semibold tracking-widest ${a.text}`}
            >
              {mod.code}
            </span>
            <span
              className={`flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider ${
                isExecuting ? a.text : result.status === "complete" ? "text-emerald-300" : result.status === "error" ? "text-rose-300" : "text-slate-500"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  isExecuting
                    ? `${a.dot} dg-ping`
                    : result.status === "complete"
                    ? "bg-emerald-400"
                    : result.status === "error"
                    ? "bg-rose-400"
                    : "bg-slate-600"
                }`}
              />
              {STATUS_LABEL[result.status]}
            </span>
          </div>

          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">
            {mod.title}
          </h2>
          <p className={`mt-1 text-xs font-medium uppercase tracking-wider ${a.text}`}>
            {mod.subtitle}
          </p>
          <p className="mt-4 text-sm leading-relaxed text-slate-400">
            {mod.description}
          </p>

          <button
            type="button"
            onClick={onRunSingle}
            disabled={isExecuting}
            className={`mt-5 inline-flex items-center gap-2 rounded-lg border ${a.border} bg-white/[0.03] px-4 py-2 text-xs font-semibold ${a.text} transition-colors hover:bg-white/[0.07] disabled:cursor-not-allowed disabled:opacity-40`}
          >
            {isExecuting ? (
              <>
                <span className="dg-spinner h-3 w-3" /> Executing&hellip;
              </>
            ) : (
              <>&#9656; Trigger module</>
            )}
          </button>
        </div>

        {/* Stat + readout column */}
        <div className="flex w-full flex-col gap-4 lg:w-72 lg:flex-none">
          <div className="rounded-xl border border-white/10 bg-black/20 p-4">
            <span className="text-[10px] font-medium uppercase tracking-wider text-white/40">
              {mod.stat.label}
            </span>
            <div className="mt-1 text-2xl font-semibold tabular-nums tracking-tight text-white">
              <CountUp
                end={mod.stat.value}
                duration={1.4}
                decimals={mod.stat.decimals ?? 0}
                prefix={mod.stat.prefix ?? ""}
                suffix={mod.stat.suffix ?? ""}
                useEasing
              />
            </div>
          </div>

          <ReadoutBox result={result} accentText={a.text} />
        </div>
      </div>
    </motion.div>
  );
}

// ── Readout — a small terminal-style panel showing raw response state ───────

function ReadoutBox({
  result,
  accentText,
}: {
  result: ModuleResult;
  accentText: string;
}) {
  const preview = useMemo(() => {
    if (result.status === "complete" && result.data) {
      const source = (result.data as { data_source?: string }).data_source ?? "unknown";
      const json = JSON.stringify(result.data, null, 2);
      return { source, json: json.length > 480 ? `${json.slice(0, 480)}\u2026` : json };
    }
    return null;
  }, [result]);

  return (
    <div className="flex-1 rounded-xl border border-white/10 bg-black/30 p-3 font-mono text-[11px] leading-relaxed">
      <div className="mb-1.5 flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-500">
        <span>Readout</span>
        {result.elapsedMs !== null && (
          <span className="text-slate-600">{result.elapsedMs}ms</span>
        )}
      </div>
      <AnimatePresence mode="wait">
        {result.status === "idle" && (
          <motion.p key="idle" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-slate-600">
            Awaiting execution&hellip;
          </motion.p>
        )}
        {result.status === "executing" && (
          <motion.p key="loading" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className={accentText}>
            &gt; querying agent telemetry&hellip;
          </motion.p>
        )}
        {result.status === "error" && (
          <motion.p key="error" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-rose-300">
            &gt; fault: {result.error}
          </motion.p>
        )}
        {result.status === "complete" && preview && (
          <motion.pre
            key="complete"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="max-h-40 overflow-hidden whitespace-pre-wrap break-all text-slate-400"
          >
            <span className={`font-semibold ${accentText}`}>source: {preview.source}</span>
            {"\n"}
            {preview.json}
          </motion.pre>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── God Mode button ──────────────────────────────────────────────────────────

function GodModeButton({
  onClick,
  active,
  completeCount,
}: {
  onClick: () => void;
  active: boolean;
  completeCount: number;
}) {
  return (
    <div className="mt-14 flex flex-col items-center gap-4 pb-4">
      <div className="relative flex w-full max-w-xl items-center justify-center">
        <div
          aria-hidden
          className="pointer-events-none absolute h-32 w-full max-w-md rounded-full blur-3xl dg-godmode-ambient"
        />
        <motion.button
          type="button"
          onClick={onClick}
          disabled={active}
          whileHover={active ? undefined : { scale: 1.015 }}
          whileTap={active ? undefined : { scale: 0.985 }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          className="dg-godmode-btn group relative z-10 flex w-full max-w-xl items-center justify-center gap-3 rounded-2xl px-10 py-6 text-base font-bold uppercase tracking-[0.15em] text-white disabled:cursor-not-allowed"
        >
          <span className="dg-godmode-border" aria-hidden />
          <span className="relative z-10 flex items-center gap-3">
            {active ? (
              <>
                <span className="dg-spinner h-5 w-5" />
                Agents Executing&hellip;
              </>
            ) : (
              <>
                <span className="text-lg leading-none">&#9889;</span>
                Initiate God Mode
                <span className="text-lg leading-none">&#9889;</span>
              </>
            )}
          </span>
        </motion.button>
      </div>
      <p className="text-[11px] uppercase tracking-widest text-slate-600">
        Runs all 5 agents concurrently &middot; {completeCount}/5 complete
      </p>
    </div>
  );
}

// ── Global / effect CSS ──────────────────────────────────────────────────────

function GlobalStyles() {
  return (
    <style jsx global>{`
      :root {
        --dg-accent: #6366f1;
        --dg-accent-2: #22d3ee;
        --glow-primary: 99, 102, 241;
        --glow-accent: 34, 211, 238;
      }

      .dg-grid {
        background-image: linear-gradient(
            to right,
            rgba(255, 255, 255, 0.03) 1px,
            transparent 1px
          ),
          linear-gradient(to bottom, rgba(255, 255, 255, 0.03) 1px, transparent 1px);
        background-size: 44px 44px;
        mask-image: radial-gradient(ellipse 80% 60% at 50% 0%, #000 40%, transparent 100%);
      }

      .dg-bloom {
        background: radial-gradient(ellipse at center, rgba(99, 102, 241, 0.2), transparent 70%);
        filter: blur(30px);
      }

      /* Faint horizontal scanlines — the one "sci-fi command deck" signature
         cue on this page. Kept extremely subtle (2% alpha) so it reads as
         texture, not a filter slapped on top. */
      .dg-scanlines {
        background: repeating-linear-gradient(
          to bottom,
          rgba(255, 255, 255, 0.02) 0px,
          rgba(255, 255, 255, 0.02) 1px,
          transparent 1px,
          transparent 3px
        );
      }

      .dg-logo {
        background: linear-gradient(135deg, var(--dg-accent), #8b5cf6);
        box-shadow: 0 0 20px -4px var(--dg-accent);
      }

      .dg-hud-panel {
        box-shadow: 0 0 0 1px rgba(var(--panel-glow), 0.06),
          0 0 50px -18px rgba(var(--panel-glow), 0.35),
          0 20px 60px -24px rgba(0, 0, 0, 0.8),
          inset 0 1px 0 rgba(255, 255, 255, 0.05);
      }

      /* HUD corner brackets — the command-center tell. Each panel gets four
         small L-shaped marks in its own accent color, like a targeting
         reticle framing the module rather than a plain rounded rectangle. */
      .dg-corner {
        position: absolute;
        width: 18px;
        height: 18px;
        border-color: rgba(var(--panel-glow), 0.55);
        pointer-events: none;
      }
      .dg-corner-tl {
        top: 10px;
        left: 10px;
        border-top: 2px solid;
        border-left: 2px solid;
        border-radius: 4px 0 0 0;
      }
      .dg-corner-tr {
        top: 10px;
        right: 10px;
        border-top: 2px solid;
        border-right: 2px solid;
        border-radius: 0 4px 0 0;
      }
      .dg-corner-bl {
        bottom: 10px;
        left: 10px;
        border-bottom: 2px solid;
        border-left: 2px solid;
        border-radius: 0 0 0 4px;
      }
      .dg-corner-br {
        bottom: 10px;
        right: 10px;
        border-bottom: 2px solid;
        border-right: 2px solid;
        border-radius: 0 0 4px 0;
      }

      .dg-godmode-ambient {
        background: radial-gradient(
          ellipse at center,
          rgba(244, 63, 94, 0.35),
          rgba(99, 102, 241, 0.25) 45%,
          transparent 75%
        );
      }

      .dg-godmode-btn {
        background: linear-gradient(180deg, #201626, #150e1c);
        box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.35), 0 12px 50px -10px rgba(244, 63, 94, 0.5),
          0 0 60px -14px rgba(99, 102, 241, 0.45);
        animation: dg-godmode-glow 2.4s ease-in-out infinite;
        overflow: hidden;
        transition: box-shadow 0.18s ease-out, opacity 0.18s ease-out;
      }
      .dg-godmode-btn:disabled {
        opacity: 0.75;
      }
      .dg-godmode-btn:hover:not(:disabled) {
        box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.6), 0 14px 60px -8px rgba(244, 63, 94, 0.7),
          0 0 70px -10px rgba(99, 102, 241, 0.6);
      }
      @keyframes dg-godmode-glow {
        0%,
        100% {
          box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.35), 0 12px 50px -10px rgba(244, 63, 94, 0.45),
            0 0 60px -14px rgba(99, 102, 241, 0.4);
        }
        50% {
          box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.55), 0 12px 58px -8px rgba(244, 63, 94, 0.65),
            0 0 70px -10px rgba(99, 102, 241, 0.6);
        }
      }
      .dg-godmode-border {
        position: absolute;
        inset: -1px;
        border-radius: 1rem;
        padding: 1px;
        background: conic-gradient(
          from var(--dg-angle, 0deg),
          transparent 0%,
          #f43f5e 20%,
          var(--dg-accent) 45%,
          var(--dg-accent-2) 65%,
          transparent 85%
        );
        -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        animation: dg-rotate 3.2s linear infinite;
      }
      @property --dg-angle {
        syntax: "<angle>";
        initial-value: 0deg;
        inherits: false;
      }
      @keyframes dg-rotate {
        to {
          --dg-angle: 360deg;
        }
      }

      .dg-spinner {
        border: 2px solid rgba(255, 255, 255, 0.25);
        border-top-color: #fff;
        border-radius: 9999px;
        animation: dg-spin 0.7s linear infinite;
      }
      @keyframes dg-spin {
        to {
          transform: rotate(360deg);
        }
      }
      .dg-ping {
        animation: dg-ping 1.6s ease-in-out infinite;
      }
      @keyframes dg-ping {
        0%,
        100% {
          opacity: 1;
          transform: scale(1);
        }
        50% {
          opacity: 0.5;
          transform: scale(0.75);
        }
      }

      @media (prefers-reduced-motion: reduce) {
        .dg-godmode-btn,
        .dg-godmode-border,
        .dg-ping,
        .dg-spinner {
          animation: none !important;
        }
      }
    `}</style>
  );
}
