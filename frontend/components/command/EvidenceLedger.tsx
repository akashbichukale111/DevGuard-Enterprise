/**
 * EvidenceLedger.tsx — the Command Center, the left rail, always visible.
 *
 * "This single panel is what separates 'real system' from 'LLM demo.'" What
 * makes that true is not the list — it is that every chip carries its
 * provenance and opens the exact bytes behind the claim.
 *
 * `UNTRUSTED_TEXT` chips are visually quarantined because the evidence rule treats catalog
 * free-text as attacker-authorable. The quarantine styling is the UI half of
 * the control the Sentinel enforces in code: a reader must be able to see, at a
 * glance, which claims came from text an attacker could have written.
 */

"use client";

import React, { useMemo, useState } from "react";
import { fmt, type EvidenceItem, type ReplayBundle } from "@/lib/replay";
import { Badge, Empty, Panel } from "./primitives";

const SOURCE_TONE: Record<string, "info" | "good" | "warn" | "quarantine" | "neutral"> = {
  RUNTIME: "good",
  DATAHUB_GRAPH: "info",
  DATAHUB_DOCUMENT: "quarantine",
  REPO_STATIC: "neutral",
  DEVGUARD_INFERENCE: "warn",
  SEEDED_DEMO: "warn",
};

/** the evidence rule's short badge text. The full source name is kept in the title attribute. */
const SOURCE_LABEL: Record<string, string> = {
  RUNTIME: "RUNTIME",
  DATAHUB_GRAPH: "DATAHUB",
  DATAHUB_DOCUMENT: "DOCUMENT",
  REPO_STATIC: "REPO",
  DEVGUARD_INFERENCE: "INFERENCE",
  SEEDED_DEMO: "SEEDED",
};

function Chip({
  item,
  onOpen,
  highlighted,
}: {
  item: EvidenceItem;
  onOpen: () => void;
  highlighted: boolean;
}) {
  const tone = SOURCE_TONE[item.source] ?? "neutral";
  return (
    <li>
      <button
        onClick={onOpen}
        className={`w-full rounded border px-2 py-1.5 text-left transition hover:brightness-125 ${
          item.untrusted
            ? "border-fuchsia-500/40 bg-fuchsia-500/[0.06]"
            : "border-white/10 bg-white/[0.02]"
        } ${item.distinct ? "border-dashed" : ""} ${
          highlighted ? "ring-1 ring-sky-400/60" : ""
        }`}
        title={`${item.source} · ${item.trust} · ${item.confidence}\ncollected ${fmt.time(
          item.collected_at,
        )}\nraw: ${item.raw_ref}`}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[9px] text-slate-500">{item.id}</span>
          <Badge tone={tone} title={item.source}>
            {SOURCE_LABEL[item.source] ?? item.source}
          </Badge>
          {item.untrusted ? (
            <Badge tone="quarantine" title="Attacker-authorable text — fenced before any prompt.">
              untrusted
            </Badge>
          ) : null}
          {item.confidence === "INFERRED" ? (
            <Badge tone="warn" title="Inferred, not observed.">
              inferred
            </Badge>
          ) : null}
        </div>
        <p
          className={`mt-1 text-[11px] leading-snug ${
            item.untrusted ? "text-fuchsia-100/80" : "text-slate-300"
          }`}
        >
          {item.claim}
        </p>
      </button>
    </li>
  );
}

export default function EvidenceLedger({
  bundle,
  onOpenRaw,
  highlightIds = [],
}: {
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
  highlightIds?: string[];
}) {
  const [filter, setFilter] = useState<string>("ALL");

  const sources = useMemo(() => {
    const counts = new Map<string, number>();
    bundle.evidence.forEach((e) => counts.set(e.source, (counts.get(e.source) ?? 0) + 1));
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [bundle.evidence]);

  const shown = useMemo(
    () => (filter === "ALL" ? bundle.evidence : bundle.evidence.filter((e) => e.source === filter)),
    [bundle.evidence, filter],
  );

  const untrustedCount = bundle.evidence.filter((e) => e.untrusted).length;

  return (
    <Panel
      title="Evidence ledger"
      subtitle={
        <>
          {bundle.evidence.length} items · digest{" "}
          <span className="font-mono text-slate-400">{bundle.chain.digest ?? "N/A"}</span>
        </>
      }
      right={
        bundle.chain.is_sufficient === false ? (
          <Badge tone="bad">chain insufficient</Badge>
        ) : (
          <Badge tone="good">chain sufficient</Badge>
        )
      }
      className="flex h-full min-h-0 flex-col"
    >
      <div className="mb-2 flex flex-wrap gap-1">
        <button
          onClick={() => setFilter("ALL")}
          className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
            filter === "ALL"
              ? "border-sky-400/50 bg-sky-500/10 text-sky-200"
              : "border-white/10 text-slate-400 hover:bg-white/5"
          }`}
        >
          ALL {bundle.evidence.length}
        </button>
        {sources.map(([src, n]) => (
          <button
            key={src}
            onClick={() => setFilter(src)}
            title={src}
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
              filter === src
                ? "border-sky-400/50 bg-sky-500/10 text-sky-200"
                : "border-white/10 text-slate-400 hover:bg-white/5"
            }`}
          >
            {SOURCE_LABEL[src] ?? src} {n}
          </button>
        ))}
      </div>

      {untrustedCount > 0 ? (
        <p className="mb-2 rounded border border-fuchsia-500/30 bg-fuchsia-500/[0.06] px-2 py-1 text-[10px] leading-snug text-fuchsia-200/80">
          {untrustedCount} item{untrustedCount === 1 ? "" : "s"} are catalog free-text and are
          quarantined as <span className="font-mono">UNTRUSTED_TEXT</span> — fenced before entering
          any prompt, never treated as instruction.
        </p>
      ) : null}

      <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
        {shown.length === 0 ? (
          <Empty>No evidence of this source in this run.</Empty>
        ) : (
          shown.map((item) => (
            <Chip
              key={item.id}
              item={item}
              highlighted={highlightIds.includes(item.id)}
              onOpen={() => onOpenRaw(item.raw_ref)}
            />
          ))
        )}
      </ul>
    </Panel>
  );
}
