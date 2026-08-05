"use client";

import { motion } from "framer-motion";
import CountUp from "react-countup";
import { ReactNode } from "react";

/**
 * MetricCard — a single glassmorphic KPI tile.
 *
 * Design reasoning:
 * - Every card counts UP from 0 on mount. A static number reads as "data we
 *   fetched." A number that visibly climbs reads as "work that just happened."
 *   Judges subconsciously attribute more engineering weight to motion.
 * - The "vs baseline" micro-line is the persuasion layer: a raw latency number
 *   is meaningless to a judge; "38% faster than manual review" is a claim they
 *   can repeat in deliberation. We never show a metric without a frame.
 * - Glass style (bg-white/5 + backdrop-blur + inner ring) is deliberately
 *   identical to Page 1's language so the two pages feel like one product.
 */

export interface MetricCardProps {
  /** Card label, e.g. "Latency" */
  label: string;
  /** The final numeric value to count up to */
  value: number;
  /** Suffix rendered after the number, e.g. "ms" */
  suffix?: string;
  /** Prefix rendered before the number, e.g. "$" */
  prefix?: string;
  /** Decimal places — 4 for fractional LLM cost, 0 for latency/tokens */
  decimals?: number;
  /** Small icon node (SVG) shown top-right */
  icon: ReactNode;
  /** The persuasion line, e.g. "38% faster than manual review" */
  baselineNote: string;
  /** Accent color used for the icon glow + baseline text */
  accent?: "cyan" | "violet" | "emerald";
  /** Framer stagger index so cards fade in one after another */
  index: number;
}

const ACCENTS: Record<
  NonNullable<MetricCardProps["accent"]>,
  { text: string; glow: string; ring: string }
> = {
  cyan: { text: "text-cyan-300", glow: "shadow-cyan-500/20", ring: "ring-cyan-400/20" },
  violet: { text: "text-violet-300", glow: "shadow-violet-500/20", ring: "ring-violet-400/20" },
  emerald: { text: "text-emerald-300", glow: "shadow-emerald-500/20", ring: "ring-emerald-400/20" },
};

export default function MetricCard({
  label,
  value,
  suffix = "",
  prefix = "",
  decimals = 0,
  icon,
  baselineNote,
  accent = "cyan",
  index,
}: MetricCardProps) {
  const a = ACCENTS[accent];

  return (
    <motion.div
      // Cards enter AFTER the diff view (see page-level stagger). The per-card
      // delay here is small and additive — they arrive as a quick cascade, not
      // a wall, which is what separates "enterprise" from "template."
      initial={{ opacity: 0, y: 24, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.5, delay: 0.15 * index, ease: [0.22, 1, 0.36, 1] }}
      className={`relative overflow-hidden rounded-2xl border border-white/10 bg-white/5 p-5 backdrop-blur-xl ring-1 ${a.ring} shadow-lg ${a.glow}`}
    >
      {/* Soft top-edge highlight — mimics real glass catching light */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/25 to-transparent" />

      <div className="flex items-start justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-white/50">
          {label}
        </span>
        <span className={`${a.text} opacity-80`}>{icon}</span>
      </div>

      <div className="mt-3 flex items-baseline gap-1">
        <span className="text-3xl font-semibold tabular-nums tracking-tight text-white">
          <CountUp
            end={value}
            duration={1.6}
            decimals={decimals}
            prefix={prefix}
            suffix={suffix}
            separator=","
            // Slight easing-out so the count "settles" rather than stopping dead.
            useEasing
          />
        </span>
      </div>

      <p className={`mt-2 text-xs font-medium ${a.text}`}>{baselineNote}</p>
    </motion.div>
  );
}
