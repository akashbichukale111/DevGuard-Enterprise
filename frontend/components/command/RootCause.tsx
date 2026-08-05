/**
 * RootCause.tsx — the prior-knowledge banner's prior-knowledge banner and the root-cause panel's root-cause panel.
 *
 * The root-cause panel: "If refused: a full-width INSUFFICIENT EVIDENCE state naming exactly
 * which evidence class was missing. This state must be as polished as the
 * success state — it is a headline demo beat, not an error screen." So the
 * refusal is rendered as a deliberate verdict with its missing class named and
 * the underlying error available, not as a red toast.
 *
 * BLOCKED is drawn as a third state, distinct from both. The Diagnostician's
 * own rationale in the dry-run packs is explicit — "the evidence chain is
 * sufficient, so this is NOT a refusal — the question was never asked" — and
 * showing a missing dependency as a governance refusal would claim credit the
 * run did not earn.
 */

"use client";

import React from "react";
import type { ReplayBundle } from "@/lib/replay";
import { Badge, Empty, NA, Panel, Urn } from "./primitives";

export function PriorKnowledge({ bundle }: { bundle: ReplayBundle }) {
  const pk = bundle.prior_knowledge;

  if (pk.state === null) {
    return (
      <div className="rounded border border-white/10 bg-[#0f0f16] px-3 py-2 text-[11px] italic text-slate-500">
        {bundle.missing["prior_knowledge"] ?? "No retrieval stage ran in this run."}
      </div>
    );
  }

  if (pk.state === "UNAVAILABLE") {
    return (
      <div className="rounded border border-amber-500/40 bg-amber-500/[0.08] px-3 py-2">
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-amber-300">
          RETRIEVAL UNAVAILABLE — NO DOCUMENTS IN CATALOG
        </div>
        <p className="mt-1 text-[11px] leading-snug text-amber-100/70">
          Capability negotiation reported the document tools are hidden. The Archivist degraded
          deliberately rather than failing. {pk.rationale}
        </p>
      </div>
    );
  }

  if (pk.state === "NONE") {
    return (
      <div className="rounded border border-slate-600/40 bg-white/[0.02] px-3 py-2">
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-slate-400">
          NO PRIOR VERIFIED INCIDENTS FOR THIS ASSET
        </div>
        <p className="mt-1 text-[11px] leading-snug text-slate-500">
          This is incident #1 for the asset. Shown rather than hidden — the loop has nothing of its
          own to retrieve yet.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded border border-emerald-500/40 bg-emerald-500/[0.07] px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-emerald-300">
          PREVIOUS VERIFIED INCIDENT
        </span>
        <Badge tone="good">{pk.document_count} runbook(s)</Badge>
        <span className="text-[11px] text-slate-400">
          time-to-root-cause:{" "}
          {pk.time_to_root_cause_s === null ? (
            <NA why={bundle.missing["prior_knowledge.time_to_root_cause_s"]} />
          ) : (
            `${pk.time_to_root_cause_s}s`
          )}
        </span>
      </div>
      <ul className="mt-1 space-y-0.5">
        {pk.documents.map((d) => (
          <li key={d.urn} className="flex flex-wrap items-baseline gap-2">
            <Urn value={d.urn} />
            {d.title ? <span className="text-[11px] text-slate-400">{d.title}</span> : null}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-[10px] italic leading-snug text-emerald-100/50">
        Written by DevGuard on an earlier run and retrieved from the catalog on this one — the loop
        reading its own knowledge back.
      </p>
    </div>
  );
}

export default function RootCause({
  bundle,
  onOpenRaw,
}: {
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  const rc = bundle.root_cause;

  if (rc.refused) {
    return (
      <Panel
        title="Root cause"
        subtitle="Refused on insufficient evidence — the loop stopped rather than guessing"
        tone="alert"
        right={<Badge tone="bad">INSUFFICIENT EVIDENCE</Badge>}
      >
        <div className="rounded border border-rose-500/50 bg-rose-500/[0.09] p-3">
          <div className="text-center font-mono text-lg font-bold uppercase tracking-[0.16em] text-rose-300">
            INSUFFICIENT EVIDENCE
          </div>
          <div className="mt-2 text-center">
            <span className="text-[11px] uppercase tracking-wide text-slate-400">
              Missing evidence class
            </span>
            <div className="mt-1 flex flex-wrap justify-center gap-1">
              {rc.missing_evidence_classes.length === 0 ? (
                <NA why="The chain was insufficient but recorded no class." />
              ) : (
                rc.missing_evidence_classes.map((c) => (
                  <span
                    key={c}
                    className="rounded border border-rose-400/50 bg-rose-500/10 px-2 py-0.5 font-mono text-xs text-rose-200"
                  >
                    {c}
                  </span>
                ))
              )}
            </div>
          </div>
          {rc.rationale ? (
            <p className="mx-auto mt-3 max-w-2xl text-center text-[11px] leading-relaxed text-rose-100/70">
              {rc.rationale}
            </p>
          ) : null}
        </div>

        {rc.datahub_error ? (
          <details className="mt-2 rounded border border-white/10 bg-black/20 p-2">
            <summary className="cursor-pointer text-[11px] text-slate-400">
              Why the graph was unavailable (captured error)
            </summary>
            <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-snug text-slate-400">
              {rc.datahub_error}
            </pre>
          </details>
        ) : null}
      </Panel>
    );
  }

  return (
    <Panel
      title="Root cause"
      subtitle={`Evidence chain · ${rc.chain_ids.length} links`}
      right={
        rc.decision === "BLOCKED" ? (
          <Badge tone="warn" title={rc.rationale ?? undefined}>
            reasoner unavailable
          </Badge>
        ) : rc.by_model ? (
          <Badge tone="info">model-authored</Badge>
        ) : (
          <Badge tone="neutral" title="Derived deterministically from typed evidence, not by an LLM.">
            deterministic
          </Badge>
        )
      }
    >
      {rc.statement ? (
        <p className="text-[13px] leading-relaxed text-slate-200">{rc.statement}</p>
      ) : (
        <Empty>{bundle.missing["root_cause.statement"] ?? "No root cause was recorded."}</Empty>
      )}

      {rc.decision === "BLOCKED" && rc.rationale ? (
        <p className="mt-2 rounded border border-orange-500/30 bg-orange-500/[0.06] px-2 py-1 text-[11px] leading-snug text-orange-200/80">
          {rc.rationale}
        </p>
      ) : null}

      <div className="mt-3">
        <h3 className="mb-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
          The chain, in order
        </h3>
        <ol className="flex flex-wrap items-center gap-1">
          {rc.chain_ids.map((id, i) => {
            const ev = bundle.evidence.find((e) => e.id === id);
            return (
              <React.Fragment key={id}>
                {i > 0 ? <span className="text-slate-700">→</span> : null}
                <li>
                  <button
                    onClick={() => ev && onOpenRaw(ev.raw_ref)}
                    title={ev?.claim ?? id}
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10px] hover:brightness-125 ${
                      ev?.untrusted
                        ? "border-fuchsia-500/40 bg-fuchsia-500/[0.08] text-fuchsia-200"
                        : "border-white/10 bg-white/[0.03] text-slate-300"
                    }`}
                  >
                    {id.replace(/^EV-[^-]+-/, "")}
                  </button>
                </li>
              </React.Fragment>
            );
          })}
        </ol>
      </div>

      {rc.raw_ref ? (
        <button
          onClick={() => onOpenRaw(rc.raw_ref as string)}
          className="mt-2 font-mono text-[10px] text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
        >
          open the recorded root-cause artifact
        </button>
      ) : null}
    </Panel>
  );
}
