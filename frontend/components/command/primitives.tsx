/**
 * primitives.tsx — the instrument-panel vocabulary the Floor panels share.
 *
 * The Command Center's visual standard: "dense, instrument-panel, readable at video
 * resolution; monospace for URNs and IDs; every number traceable to an
 * artifact. Not a marketing landing page." These are the pieces that keep that
 * consistent across eleven panels without each one inventing its own spacing.
 */

"use client";

import React from "react";

export function Panel({
  title,
  subtitle,
  right,
  children,
  tone = "default",
  className = "",
}: {
  title: string;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
  tone?: "default" | "alert" | "good";
  className?: string;
}) {
  const border =
    tone === "alert"
      ? "border-amber-500/40"
      : tone === "good"
        ? "border-emerald-500/30"
        : "border-white/10";
  return (
    <section className={`rounded border ${border} bg-[#0f0f16] ${className}`}>
      <header className="flex items-baseline justify-between gap-3 border-b border-white/10 px-3 py-2">
        <div className="min-w-0">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-300">
            {title}
          </h2>
          {subtitle ? <p className="mt-0.5 text-[11px] text-slate-500">{subtitle}</p> : null}
        </div>
        {right ? <div className="shrink-0 text-[11px] text-slate-400">{right}</div> : null}
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}

/** A value with its label. `na` styling is deliberately visible, not hidden. */
export function Stat({
  label,
  value,
  hint,
  na,
}: {
  label: string;
  value: string;
  hint?: string | null;
  na?: boolean;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">{label}</div>
      <div
        className={`font-mono text-lg leading-tight ${na ? "text-slate-600" : "text-slate-100"}`}
        title={hint ?? undefined}
      >
        {value}
      </div>
      {hint ? <div className="mt-0.5 text-[10px] leading-tight text-slate-500">{hint}</div> : null}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad" | "info" | "quarantine";
  title?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "border-white/15 bg-white/5 text-slate-300",
    good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    warn: "border-amber-500/40 bg-amber-500/10 text-amber-300",
    bad: "border-rose-500/40 bg-rose-500/10 text-rose-300",
    info: "border-sky-500/40 bg-sky-500/10 text-sky-300",
    quarantine: "border-fuchsia-500/50 bg-fuchsia-500/10 text-fuchsia-300",
  };
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/** Monospace URN. Long ones truncate visually but keep the full value on hover. */
export function Urn({
  value,
  href,
  className = "",
}: {
  value: string | null | undefined;
  href?: string | null;
  className?: string;
}) {
  if (!value) return <span className="font-mono text-xs text-slate-600">N/A</span>;
  const body = (
    <span className={`font-mono text-xs text-slate-300 ${className}`} title={value}>
      {value}
    </span>
  );
  if (!href) return body;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="font-mono text-xs text-sky-300 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-200"
      title={value}
    >
      {value}
    </a>
  );
}

/** The N/A renderer. Anything unknown goes through here so it reads the same. */
export function NA({ why }: { why?: string | null }) {
  return (
    <span
      className="cursor-help font-mono text-xs text-slate-600 underline decoration-dotted decoration-slate-700 underline-offset-2"
      title={why ?? "Not captured in this run"}
    >
      N/A
    </span>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-2 text-xs italic leading-relaxed text-slate-500">{children}</p>;
}
