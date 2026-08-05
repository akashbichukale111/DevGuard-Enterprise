// frontend/app/page.tsx
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AnimatePresence,
  motion,
  useMotionValue,
  useSpring,
  useTransform,
} from "framer-motion";
import {
  Activity,
  ArrowRight,
  Bug,
  Code2,
  GitBranch,
  Radar as RadarIcon,
  ShieldCheck,
  Sparkles,
  Terminal as TerminalIcon,
} from "lucide-react";

// ═════════════════════════════════════════════════════════════════════════
// DevGuard AI — Landing / Gateway Page
// -----------------------------------------------------------------------
// This is intentionally NOT a scrolling marketing page. It's a single-
// viewport "gateway console": boot up, confirm the mesh is alive, then
// present exactly one decision — which seat do you want, Developer or SRE?
// Everything lives above the fold; the two destinations (/scanner, /nexus)
// carry their own deep-dive content, so this page's only job is routing
// with maximum ceremony on the way out.
//
// Palette: #05050a void, cyan-core (#06b6d4) = Developer/Scanner track,
// violet/rose (#818cf8 / #f43f5e) = SRE/Nexus track — the two accent
// families double as a color-coded fork in the road.
// ═════════════════════════════════════════════════════════════════════════

const BOOT_LINES: { text: string; tone: "system" | "network" | "ai" | "auth" }[] = [
  { text: "[SYSTEM] Initializing DevGuard AI kernel v2.4.1...", tone: "system" },
  { text: "[NETWORK] Connecting to SigNoz Mesh (self-hosted, OTLP/gRPC)...", tone: "network" },
  { text: "[AI] Booting Scanner \u2192 Fixer \u2192 Validator reflection loop...", tone: "ai" },
  { text: "[AI] 5 Nexus agents online: Omni-Heal, Pre-Cog, FinOps, Truth Serum, Exec Commander", tone: "ai" },
  { text: "[AUTH] Access Granted — welcome back, operator.", tone: "auth" },
];

// ─────────────────────────────────────────────────────────────────────────
// Utility hooks
// ─────────────────────────────────────────────────────────────────────────

function useMounted() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

function useLineTypewriter(lines: string[], msPerChar = 13, lineGapMs = 90, onDone?: () => void) {
  const [rendered, setRendered] = useState<string[]>([]);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const key = lines.join("|");

  useEffect(() => {
    let cancelled = false;
    let lineIdx = 0;
    let charIdx = 0;
    setRendered([]);

    function tick() {
      if (cancelled || lineIdx >= lines.length) {
        if (!cancelled) doneRef.current?.();
        return;
      }
      const line = lines[lineIdx];
      charIdx += 1;
      setRendered((prev) => {
        const next = [...prev];
        next[lineIdx] = line.slice(0, charIdx);
        return next;
      });
      if (charIdx >= line.length) {
        lineIdx += 1;
        charIdx = 0;
        setTimeout(tick, lineGapMs);
      } else {
        setTimeout(tick, msPerChar);
      }
    }
    const starter = setTimeout(tick, 100);
    return () => {
      cancelled = true;
      clearTimeout(starter);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return rendered;
}

/** 3D tilt + cursor-relative glow position for the two gateway cards. */
function useTilt(strength = 7) {
  const ref = useRef<HTMLDivElement>(null);
  const px = useMotionValue(0.5);
  const py = useMotionValue(0.5);
  const springX = useSpring(px, { stiffness: 200, damping: 22, mass: 0.4 });
  const springY = useSpring(py, { stiffness: 200, damping: 22, mass: 0.4 });

  const rotateX = useTransform(springY, [0, 1], [strength, -strength]);
  const rotateY = useTransform(springX, [0, 1], [-strength, strength]);
  const glowX = useTransform(springX, [0, 1], ["0%", "100%"]);
  const glowY = useTransform(springY, [0, 1], ["0%", "100%"]);
  const bg = useTransform([glowX, glowY], (v) => {
    const [gx, gy] = v as [string, string];
    return `radial-gradient(480px circle at ${gx} ${gy}, var(--card-glow), transparent 62%)`;
  });

  const onMouseMove = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const rect = ref.current?.getBoundingClientRect();
      if (!rect) return;
      px.set((e.clientX - rect.left) / rect.width);
      py.set((e.clientY - rect.top) / rect.height);
    },
    [px, py]
  );

  const onMouseLeave = useCallback(() => {
    px.set(0.5);
    py.set(0.5);
  }, [px, py]);

  return { ref, rotateX, rotateY, bg, onMouseMove, onMouseLeave };
}

// ═════════════════════════════════════════════════════════════════════════
// Boot sequence overlay
// ═════════════════════════════════════════════════════════════════════════

function BootSequence({ onComplete }: { onComplete: () => void }) {
  const [typedDone, setTypedDone] = useState(false);
  const [exiting, setExiting] = useState(false);
  const lineTexts = useMemo(() => BOOT_LINES.map((l) => l.text), []);
  const rendered = useLineTypewriter(lineTexts, 11, 130, () => setTypedDone(true));

  useEffect(() => {
    if (!typedDone) return;
    const t = setTimeout(() => setExiting(true), 600);
    return () => clearTimeout(t);
  }, [typedDone]);

  useEffect(() => {
    if (!exiting) return;
    const t = setTimeout(onComplete, 600);
    return () => clearTimeout(t);
  }, [exiting, onComplete]);

  const toneColor: Record<(typeof BOOT_LINES)[number]["tone"], string> = {
    system: "text-slate-400",
    network: "text-cyan-300",
    ai: "text-indigo-300",
    auth: "text-emerald-300",
  };

  return (
    <motion.div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black"
      initial={{ opacity: 1 }}
      animate={exiting ? { opacity: 0, scale: 1.06 } : { opacity: 1, scale: 1 }}
      transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
    >
      <div className="pointer-events-none absolute inset-0 dg-scanlines opacity-[0.4]" />
      <div className="pointer-events-none absolute inset-0 dg-boot-vignette" />
      <div className="relative w-full max-w-2xl px-6">
        <div className="mb-3 flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-slate-600">
          <TerminalIcon className="h-3.5 w-3.5" />
          <span>devguard-kernel &mdash; boot console</span>
        </div>
        <div className="rounded-xl border border-white/10 bg-white/[0.02] p-5 font-mono text-[13px] leading-relaxed shadow-[0_0_80px_-20px_rgba(6,182,212,0.35)]">
          {BOOT_LINES.map((l, i) => (
            <div key={i} className={`${toneColor[l.tone]} min-h-[1.4em]`}>
              {rendered[i] ?? ""}
              {rendered[i]?.length === l.text.length ? null : <span className="dg-caret" aria-hidden />}
            </div>
          ))}
        </div>
      </div>
    </motion.div>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// Live telemetry header bar
// ═════════════════════════════════════════════════════════════════════════

// Real platform status, replacing the invented "Threats Blocked" counter and
// random-walk latency figure that used to live here. Every value below comes
// from an actual round trip to the backend's /slo-status endpoint, and the
// measured latency is this browser's real round-trip time for that request.
// Anything the backend does not report renders N/A. Nothing here is animated
// into existence.

type BackendState = "checking" | "connected" | "offline";

interface PlatformStatus {
  state: BackendState;
  rttMs: number | null;
  slo: number | null;
  breaker: string | null;
}

const STATUS_API =
  process.env.NEXT_PUBLIC_API_BASE ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function LiveTelemetryBar() {
  const mounted = useMounted();
  const [status, setStatus] = useState<PlatformStatus>({
    state: "checking",
    rttMs: null,
    slo: null,
    breaker: null,
  });

  useEffect(() => {
    if (!mounted) return;
    let cancelled = false;

    const check = async () => {
      const started = performance.now();
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4000);
        const res = await fetch(`${STATUS_API}/slo-status`, { signal: controller.signal });
        clearTimeout(timeout);
        const rttMs = Math.round(performance.now() - started);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (cancelled) return;
        setStatus({
          state: "connected",
          rttMs,
          slo: typeof body?.current_compliance_pct === "number" ? body.current_compliance_pct : null,
          breaker:
            typeof body?.circuit_breaker?.state === "string" ? body.circuit_breaker.state : null,
        });
      } catch {
        if (cancelled) return;
        setStatus({ state: "offline", rttMs: null, slo: null, breaker: null });
      }
    };

    check();
    // 15s is frequent enough to feel live without hammering the backend during
    // a demo. Nothing re-renders between polls.
    const id = setInterval(check, 15000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [mounted]);

  const dot =
    status.state === "connected"
      ? "bg-emerald-400"
      : status.state === "offline"
      ? "bg-rose-400"
      : "bg-slate-500";
  const word =
    status.state === "connected"
      ? "Backend connected"
      : status.state === "offline"
      ? "Backend offline"
      : "Checking backend…";

  return (
    <div className="relative z-40 flex-none border-b border-white/[0.06] bg-black/50 backdrop-blur-xl">
      <div className="mx-auto flex h-11 max-w-7xl items-center justify-between px-4 sm:px-6">
        <div className="flex items-center gap-2">
          <div className="dg-logo-mini flex h-6 w-6 items-center justify-center rounded-md text-[11px] font-bold text-white">
            D
          </div>
          <span className="text-[13px] font-semibold tracking-tight text-white">
            DevGuard<span className="text-cyan-400">.</span>AI
          </span>
        </div>

        <div className="hidden items-center gap-6 font-mono text-[11px] text-slate-400 sm:flex">
          {/* Colour is never the only signal — the word is always present. */}
          <span className="flex items-center gap-1.5">
            <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${dot}`} />
            {word}
          </span>
          <span>
            Round trip:{" "}
            <span className="text-cyan-300">
              {status.rttMs === null ? "N/A" : `${status.rttMs}ms`}
            </span>
          </span>
          <span>
            SLO:{" "}
            <span className="text-emerald-300">
              {status.slo === null ? "N/A" : `${status.slo.toFixed(1)}%`}
            </span>
          </span>
          <span>
            Breaker: <span className="text-slate-300">{status.breaker ?? "N/A"}</span>
          </span>
        </div>

        <span className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          <ShieldCheck className="h-3 w-3" />
          {status.state === "connected" ? "Live" : status.state === "offline" ? "Offline" : "…"}
        </span>
      </div>
    </div>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// Full-screen navigation transition — the "khup bhari effect" on click
// ═════════════════════════════════════════════════════════════════════════

interface TransitionSpec {
  label: string;
  accent: "cyan" | "rose";
}

function NavigationTransition({ spec }: { spec: TransitionSpec }) {
  const isCyan = spec.accent === "cyan";
  return (
    <motion.div
      className="fixed inset-0 z-[200] flex items-center justify-center overflow-hidden bg-black"
      initial={{ clipPath: "circle(0% at var(--ox) var(--oy))" }}
      animate={{ clipPath: "circle(150% at var(--ox) var(--oy))" }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.65, ease: [0.76, 0, 0.24, 1] }}
    >
      <div className={`pointer-events-none absolute inset-0 ${isCyan ? "dg-wipe-cyan" : "dg-wipe-rose"}`} />
      <div className="pointer-events-none absolute inset-0 dg-scanlines opacity-30" />
      <motion.div
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ delay: 0.2, duration: 0.4 }}
        className="relative flex flex-col items-center gap-3"
      >
        <span className="dg-spinner h-8 w-8" />
        <p className="font-mono text-sm uppercase tracking-[0.3em] text-white">{spec.label}</p>
      </motion.div>
    </motion.div>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// Gateway action card
// ═════════════════════════════════════════════════════════════════════════

interface ActionCardDef {
  href: string;
  eyebrow: string;
  title: string;
  subtitle: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  accent: "cyan" | "rose";
  stats: { label: string; value: string }[];
  transitionLabel: string;
}

const ACCENT_STYLES: Record<
  "cyan" | "rose",
  { text: string; border: string; ring: string; glow: string; conic: string }
> = {
  cyan: {
    text: "text-cyan-300",
    border: "border-cyan-400/25",
    ring: "ring-cyan-400/15",
    glow: "rgba(34,211,238,0.16)",
    conic:
      "conic-gradient(from 0deg, transparent 0%, rgba(34,211,238,0.65) 20%, transparent 40%, rgba(79,70,229,0.4) 60%, transparent 80%, rgba(34,211,238,0.5) 100%)",
  },
  rose: {
    text: "text-rose-300",
    border: "border-rose-400/25",
    ring: "ring-rose-400/15",
    glow: "rgba(244,63,94,0.16)",
    conic:
      "conic-gradient(from 0deg, transparent 0%, rgba(244,63,94,0.65) 20%, transparent 40%, rgba(167,139,250,0.4) 60%, transparent 80%, rgba(244,63,94,0.5) 100%)",
  },
};

function ActionCard({
  def,
  onNavigate,
}: {
  def: ActionCardDef;
  onNavigate: (href: string, spec: TransitionSpec, origin: { x: number; y: number }) => void;
}) {
  const { ref, rotateX, rotateY, bg, onMouseMove, onMouseLeave } = useTilt(5);
  const a = ACCENT_STYLES[def.accent];
  const Icon = def.icon;

  const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    onNavigate(
      def.href,
      { label: def.transitionLabel, accent: def.accent },
      { x: e.clientX, y: e.clientY }
    );
  };

  return (
    <motion.div
      ref={ref}
      onMouseMove={onMouseMove}
      onMouseLeave={onMouseLeave}
      style={{ rotateX, rotateY, transformPerspective: 1000, ["--card-glow" as string]: a.glow }}
      whileHover={{ y: -4 }}
      whileTap={{ scale: 0.985 }}
      className={`dg-gateway-card group relative flex h-full flex-col overflow-hidden rounded-2xl border ${a.border} bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ${a.ring} sm:p-8`}
    >
      <div
        aria-hidden
        className="dg-card-conic pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-500 group-hover:opacity-100"
        style={{ ["--conic" as string]: a.conic }}
      />
      <motion.div aria-hidden className="pointer-events-none absolute inset-0" style={{ background: bg }} />

      <div className="relative z-10 flex flex-1 flex-col">
        <div className="flex items-center justify-between">
          <div className={`flex h-11 w-11 items-center justify-center rounded-xl border ${a.border} bg-white/[0.04]`}>
            <Icon className={`h-5 w-5 ${a.text}`} />
          </div>
          <span className={`font-mono text-[10px] uppercase tracking-[0.2em] ${a.text}`}>{def.eyebrow}</span>
        </div>

        <h3 className="mt-5 text-2xl font-bold tracking-tight text-white sm:text-[26px]">{def.title}</h3>
        <p className={`mt-1 text-xs font-semibold uppercase tracking-wider ${a.text}`}>{def.subtitle}</p>
        <p className="mt-3 max-w-sm text-[13.5px] leading-relaxed text-slate-400">{def.description}</p>

        <div className="mt-5 flex flex-wrap gap-2">
          {def.stats.map((s) => (
            <span
              key={s.label}
              className="rounded-lg border border-white/10 bg-black/30 px-2.5 py-1 font-mono text-[10.5px] text-slate-400"
            >
              {s.label}: <span className="text-white">{s.value}</span>
            </span>
          ))}
        </div>

        <button
          type="button"
          onClick={handleClick}
          className={`dg-enter-btn mt-6 flex items-center justify-between rounded-xl border ${a.border} bg-white/[0.03] px-5 py-3.5 text-left transition-colors hover:bg-white/[0.08]`}
        >
          <span className="text-sm font-semibold text-white">Enter {def.title}</span>
          <ArrowRight className={`h-4 w-4 ${a.text} transition-transform group-hover:translate-x-1`} />
        </button>
      </div>
    </motion.div>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// Page
// ═════════════════════════════════════════════════════════════════════════

const ACTION_CARDS: ActionCardDef[] = [
  {
    href: "/scanner",
    eyebrow: "Developer Mode",
    title: "Code Scanner",
    subtitle: "Scan \u2192 Fix \u2192 Validate a snippet",
    description:
      "Paste vulnerable code and watch the Scanner, Fixer, and Validator agents detect, patch, and adversarially prove the fix \u2014 live, with full trace visibility.",
    icon: Code2,
    accent: "cyan",
    stats: [
      { label: "CWE classes", value: "14+" },
      { label: "Max retries", value: "3" },
    ],
    transitionLabel: "Booting Code Scanner\u2026",
  },
  {
    href: "/nexus",
    eyebrow: "SRE God Mode",
    title: "Nexus Commander",
    subtitle: "Command all 5 agents at once",
    description:
      "Trigger Omni-Heal, FinOps, Pre-Cog, Truth Serum, and the Executive Commander concurrently \u2014 the full self-observing mesh, one command deck.",
    icon: RadarIcon,
    accent: "rose",
    stats: [
      { label: "Agents", value: "5" },
      { label: "Mode", value: "Concurrent" },
    ],
    transitionLabel: "Engaging Nexus Command Center\u2026",
  },
];

export default function LandingPage() {
  const router = useRouter();
  const [booted, setBooted] = useState(false);
  const [transition, setTransition] = useState<{ spec: TransitionSpec; origin: { x: number; y: number } } | null>(
    null
  );

  const handleNavigate = useCallback(
    (href: string, spec: TransitionSpec, origin: { x: number; y: number }) => {
      setTransition({ spec, origin });
      setTimeout(() => router.push(href), 640);
    },
    [router]
  );

  return (
    <>
      <GlobalStyles />
      <AnimatePresence>{!booted && <BootSequence onComplete={() => setBooted(true)} />}</AnimatePresence>
      <AnimatePresence>
        {transition && (
          <div
            style={
              {
                "--ox": `${transition.origin.x}px`,
                "--oy": `${transition.origin.y}px`,
              } as React.CSSProperties
            }
          >
            <NavigationTransition spec={transition.spec} />
          </div>
        )}
      </AnimatePresence>

      <main className="relative flex h-[100dvh] w-full flex-col overflow-hidden bg-[#05050a] text-slate-200 antialiased">
        <div className="pointer-events-none absolute inset-0 dg-grid opacity-[0.18]" />
        <div className="pointer-events-none absolute -top-32 left-1/2 h-[560px] w-[900px] -translate-x-1/2 rounded-full dg-bloom" />
        <div className="pointer-events-none absolute inset-0 dg-scanlines opacity-[0.2]" />

        <LiveTelemetryBar />

        <motion.div
          initial={{ opacity: 0 }}
          animate={booted ? { opacity: 1 } : { opacity: 0 }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1], delay: booted ? 0.05 : 0 }}
          className="relative z-10 mx-auto flex w-full max-w-6xl flex-1 flex-col items-center justify-center px-4 py-6 sm:px-6"
        >
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
            className="dg-badge inline-flex items-center gap-2 rounded-full border border-cyan-400/20 bg-cyan-400/[0.06] px-4 py-1.5 text-[11px] font-medium text-cyan-200"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Self-observing AI security pipeline
          </motion.div>

          <motion.h1
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.28, duration: 0.65, ease: [0.22, 1, 0.36, 1] }}
            className="mt-5 max-w-3xl text-center text-3xl font-bold tracking-tight text-white sm:text-5xl lg:text-[52px]"
          >
            <span className="dg-gradient-text">Autonomous AI Security</span>
            <br />
            <span className="dg-gradient-text-2">&amp; Observability Mesh</span>
          </motion.h1>

          <motion.p
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.4, duration: 0.65, ease: [0.22, 1, 0.36, 1] }}
            className="mt-4 max-w-lg text-center text-sm leading-relaxed text-slate-400 sm:text-base"
          >
            Choose your seat. Every path runs through the same self-observing
            pipeline &mdash; traced end-to-end in SigNoz.
          </motion.p>

          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.52, duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
            className="mt-8 grid w-full grid-cols-1 gap-5 sm:mt-10 sm:grid-cols-2 sm:gap-6"
          >
            {ACTION_CARDS.map((def) => (
              <ActionCard key={def.href} def={def} onNavigate={handleNavigate} />
            ))}
          </motion.div>

          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.75, duration: 0.7 }}
            className="mt-6 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-[10.5px] uppercase tracking-widest text-slate-600 sm:mt-8"
          >
            <span className="flex items-center gap-1.5">
              <ShieldCheck className="h-3.5 w-3.5" /> Hash-chained audit trail
            </span>
            <span className="flex items-center gap-1.5">
              <GitBranch className="h-3.5 w-3.5" /> 3-retry reflection loop
            </span>
            <span className="flex items-center gap-1.5">
              <Activity className="h-3.5 w-3.5" /> Full OTel tracing
            </span>
            <span className="flex items-center gap-1.5">
              <Bug className="h-3.5 w-3.5" /> 14+ CWE classes covered
            </span>
          </motion.div>
        </motion.div>
      </main>
    </>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// Global styles
// ═════════════════════════════════════════════════════════════════════════

function GlobalStyles() {
  return (
    <style jsx global>{`
      html,
      body {
        background: #000;
        height: 100%;
        overscroll-behavior: none;
      }
      @media (min-width: 640px) {
        html,
        body {
          overflow: hidden;
        }
      }

      .dg-grid {
        background-image: linear-gradient(to right, rgba(255, 255, 255, 0.035) 1px, transparent 1px),
          linear-gradient(to bottom, rgba(255, 255, 255, 0.035) 1px, transparent 1px);
        background-size: 42px 42px;
        mask-image: radial-gradient(ellipse 85% 60% at 50% 0%, #000 40%, transparent 100%);
      }

      .dg-bloom {
        background: radial-gradient(ellipse at center, rgba(79, 70, 229, 0.22), transparent 70%);
        filter: blur(40px);
      }

      .dg-boot-vignette {
        background: radial-gradient(ellipse at center, transparent 40%, rgba(0, 0, 0, 0.9) 100%);
      }

      .dg-scanlines {
        background: repeating-linear-gradient(
          to bottom,
          rgba(255, 255, 255, 0.018) 0px,
          rgba(255, 255, 255, 0.018) 1px,
          transparent 1px,
          transparent 3px
        );
      }

      .dg-logo-mini {
        background: linear-gradient(135deg, #4f46e5, #06b6d4);
        box-shadow: 0 0 16px -2px #06b6d4;
      }

      .dg-gradient-text {
        background: linear-gradient(90deg, #ffffff 0%, #a5f3fc 45%, #22d3ee 100%);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
      }
      .dg-gradient-text-2 {
        background: linear-gradient(90deg, #c7d2fe 0%, #818cf8 50%, #4f46e5 100%);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
      }

      .dg-badge {
        box-shadow: 0 0 0 1px rgba(34, 211, 238, 0.08), 0 0 30px -10px rgba(34, 211, 238, 0.5);
      }

      .dg-caret {
        display: inline-block;
        width: 7px;
        height: 1em;
        margin-left: 1px;
        background: currentColor;
        vertical-align: text-bottom;
        animation: dg-blink 0.9s steps(1) infinite;
      }
      @keyframes dg-blink {
        50% {
          opacity: 0;
        }
      }

      .dg-spinner {
        display: inline-block;
        border: 3px solid rgba(255, 255, 255, 0.2);
        border-top-color: #fff;
        border-radius: 9999px;
        animation: dg-spin 0.7s linear infinite;
      }
      @keyframes dg-spin {
        to {
          transform: rotate(360deg);
        }
      }

      .dg-gateway-card {
        box-shadow: 0 1px 0 rgba(255, 255, 255, 0.04) inset, 0 24px 70px -30px rgba(0, 0, 0, 0.9);
        transition: border-color 0.25s ease-out, box-shadow 0.25s ease-out;
        min-height: 300px;
      }
      .dg-gateway-card:hover {
        box-shadow: 0 1px 0 rgba(255, 255, 255, 0.06) inset, 0 30px 90px -26px rgba(0, 0, 0, 0.95);
      }

      .dg-card-conic {
        border-radius: inherit;
        padding: 1px;
        background: var(--conic);
        -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        animation: dg-rotate 4s linear infinite;
      }
      @keyframes dg-rotate {
        to {
          transform: rotate(360deg);
        }
      }

      .dg-enter-btn {
        position: relative;
        overflow: hidden;
      }
      .dg-enter-btn::after {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(100deg, transparent 20%, rgba(255, 255, 255, 0.12) 50%, transparent 80%);
        transform: translateX(-120%);
        transition: transform 0.6s ease-out;
      }
      .dg-enter-btn:hover::after {
        transform: translateX(120%);
      }

      .dg-wipe-cyan {
        background: radial-gradient(circle at var(--ox) var(--oy), rgba(34, 211, 238, 0.35), #05050a 65%);
      }
      .dg-wipe-rose {
        background: radial-gradient(circle at var(--ox) var(--oy), rgba(244, 63, 94, 0.35), #05050a 65%);
      }

      @media (prefers-reduced-motion: reduce) {
        .dg-card-conic,
        .dg-caret,
        .dg-spinner {
          animation: none !important;
        }
      }
    `}</style>
  );
}
