/**
 * WriteBack.tsx — the Command Center, the money shot.
 *
 * Five rows, each with its outcome, the live DataHub URN and the evidence that
 * justified it, plus the Command Center's guard rendered as UI text: *nothing is written
 * until recovery is verified.* The guard is not a slogan here — it is the
 * ordering the Scribe actually enforces, and stating it next to the artifacts
 * is what makes the panel a claim a judge can check rather than admire.
 *
 * `already_present` is shown as a success, not a failure. It is what idempotency
 * looks like on a second run over the same incident, and the loop was run twice
 * from clean state precisely to produce it.
 */

"use client";

import React from "react";
import type { ReplayBundle, WriteBackArtifact } from "@/lib/replay";
import { Badge, Empty, Panel, Urn } from "./primitives";

const OUTCOME_TONE: Record<string, "good" | "warn" | "bad" | "info" | "neutral"> = {
  written: "good",
  already_present: "info",
  pending: "neutral",
  dry_run: "warn",
  skipped_dry_run: "warn",
  failed: "bad",
};

function Row({
  art,
  bundle,
  onOpenRaw,
}: {
  art: WriteBackArtifact;
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  const tone = OUTCOME_TONE[art.outcome] ?? "neutral";
  const hasRaw = Boolean(art.raw_ref && bundle.raw[art.raw_ref]);
  return (
    <tr className="border-t border-white/5 align-top">
      <td className="py-1.5 pr-2 font-mono text-[11px] text-slate-500">{art.number}</td>
      <td className="py-1.5 pr-2 text-[11px] text-slate-200">
        {art.name}
        {art.detail && !art.detail.startsWith("urn:li:") ? (
          <div className="text-[10px] text-slate-500">{art.detail}</div>
        ) : null}
      </td>
      <td className="py-1.5 pr-2">
        <Badge tone={tone}>{art.outcome.replace(/_/g, " ")}</Badge>
      </td>
      <td className="min-w-0 py-1.5 pr-2">
        <div className="break-all">
          <Urn value={art.created_urn ?? art.target_urn} href={art.datahub_link} />
        </div>
      </td>
      <td className="py-1.5 text-right">
        {hasRaw ? (
          <button
            onClick={() => onOpenRaw(art.raw_ref)}
            className="font-mono text-[10px] text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
          >
            payload
          </button>
        ) : (
          <span className="font-mono text-[10px] text-slate-700">—</span>
        )}
      </td>
    </tr>
  );
}

export default function WriteBack({
  bundle,
  onOpenRaw,
}: {
  bundle: ReplayBundle;
  onOpenRaw: (ref: string) => void;
}) {
  const wb = bundle.write_back;
  if (!wb) {
    return (
      <Panel title="Write-back">
        <Empty>{bundle.missing["write_back"] ?? "The run stopped before the Scribe."}</Empty>
      </Panel>
    );
  }

  const landed = wb.artifacts.filter((a) => ["written", "already_present"].includes(a.outcome));

  return (
    <Panel
      title="Write-back to DataHub"
      subtitle={`${wb.artifacts.length} artifacts · ${landed.length} landed`}
      tone={wb.all_landed && landed.length > 0 ? "good" : "default"}
      right={
        <div className="flex items-center gap-1">
          {wb.incident_resolved ? (
            <Badge tone="good">incident resolved</Badge>
          ) : (
            <Badge tone="warn">incident open</Badge>
          )}
        </div>
      }
    >
      {wb.reason ? (
        <p className="mb-2 rounded border border-sky-500/30 bg-sky-500/[0.06] px-2 py-1 text-[11px] leading-snug text-sky-200/80">
          {wb.reason}
        </p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
              <th className="pb-1 pr-2 font-normal">#</th>
              <th className="pb-1 pr-2 font-normal">Artifact</th>
              <th className="pb-1 pr-2 font-normal">Status</th>
              <th className="pb-1 pr-2 font-normal">DataHub URN</th>
              <th className="pb-1 text-right font-normal">Raw</th>
            </tr>
          </thead>
          <tbody>
            {wb.artifacts.map((a) => (
              <Row key={`${a.number}-${a.target_urn}`} art={a} bundle={bundle} onOpenRaw={onOpenRaw} />
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2 border-t border-white/10 pt-2 text-[11px] italic leading-snug text-emerald-300/80">
        {wb.guard}
      </p>
    </Panel>
  );
}
