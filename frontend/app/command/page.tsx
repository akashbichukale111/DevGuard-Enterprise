/**
 * /command — the DevGuard Command Center (the Command Center), in replay mode (the zero-infrastructure replay requirement).
 *
 * The zero-infrastructure replay requirement: "`--replay <run-id>` drives this exact UI from the proof pack with
 * **zero infrastructure** — no DataHub, no LLM key, no Postgres."
 *
 * That is literal here. This page is a client component that fetches two static
 * JSON files from `public/replay/` and renders entirely in the browser. There
 * is no API route, no server component doing data access, and nothing that
 * reads the filesystem at request time, which is what lets the whole thing ship
 * as a static export a judge can open from a URL or a local file server.
 *
 * The run is selected with `?run=<run-id>`, so a link can point at one specific
 * recorded run — including the refusal, which is a headline beat and not an
 * error page.
 */

"use client";

import React, { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { ManifestEntry, ReplayBundle } from "@/lib/replay";
import IncidentHeader from "@/components/command/IncidentHeader";
import HandoffRail from "@/components/command/HandoffRail";
import EvidenceLedger from "@/components/command/EvidenceLedger";
import BlastRadiusPanel from "@/components/command/BlastRadius";
import RootCause, { PriorKnowledge } from "@/components/command/RootCause";
import PolicyApproval from "@/components/command/PolicyApproval";
import WriteBack from "@/components/command/WriteBack";
import CatalogSurface from "@/components/command/CatalogSurface";
import { MetricsStrip, MissingLedger, SecurityPanel } from "@/components/command/SecurityMetrics";
import RawViewer from "@/components/command/RawViewer";

/**
 * Static exports are served from a path prefix in some hosting setups, so the
 * fetch base is taken from the same place Next takes it. Falls back to a
 * document-relative path, which is what makes `file://` and `python -m
 * http.server` both work without configuration.
 */
const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

function RunPicker({
  runs,
  current,
  onPick,
}: {
  runs: ManifestEntry[];
  current: string;
  onPick: (id: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Recorded run</span>
      {runs.map((r) => (
        <button
          key={r.run_id}
          onClick={() => onPick(r.run_id)}
          title={`${r.headline} · ${r.evidence_count} evidence · ${r.artifact_count ?? "?"} artifacts`}
          className={`rounded border px-2 py-0.5 font-mono text-[10px] transition ${
            r.run_id === current
              ? "border-sky-400/60 bg-sky-500/10 text-sky-200"
              : "border-white/10 text-slate-400 hover:bg-white/5"
          }`}
        >
          {r.run_id}
          {r.refused ? <span className="ml-1 text-rose-400">refusal</span> : null}
          {r.dry_run ? <span className="ml-1 text-amber-400">dry</span> : null}
        </button>
      ))}
    </div>
  );
}

function CommandCenter() {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("run");

  const [manifest, setManifest] = useState<{ runs: ManifestEntry[]; default: string | null } | null>(
    null,
  );
  const [bundle, setBundle] = useState<ReplayBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rawRef, setRawRef] = useState<string | null>(null);

  /**
   * The run to show: the one asked for, else the manifest's default. Derived
   * rather than stored, so switching runs is a URL change and nothing has to
   * clear stale state synchronously — `bundle` is simply not the current run
   * yet, which is what `loading` below reads.
   */
  const runId = requested ?? manifest?.default ?? null;

  useEffect(() => {
    let cancelled = false;
    fetch(`${BASE}/replay/manifest.json`)
      .then((r) => {
        if (!r.ok) throw new Error(`manifest.json returned ${r.status}`);
        return r.json();
      })
      .then((m: { runs: ManifestEntry[]; default: string | null }) => {
        if (!cancelled) setManifest(m);
      })
      .catch((e: Error) => {
        if (!cancelled)
          setError(
            `Could not load the replay manifest (${e.message}). Run \`make replay\` to build ` +
              `the bundles from the committed proof packs.`,
          );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    fetch(`${BASE}/replay/${runId}.json`)
      .then((r) => {
        if (!r.ok) throw new Error(`${runId}.json returned ${r.status}`);
        return r.json();
      })
      .then((b: ReplayBundle) => {
        if (!cancelled) {
          setBundle(b);
          setError(null);
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setError(`Could not load run ${runId} (${e.message}).`);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const pick = (id: string) => router.push(`/command?run=${encodeURIComponent(id)}`);
  const loading = !bundle || (runId !== null && bundle.run_id !== runId);

  if (error) {
    return (
      <main className="mx-auto max-w-3xl p-8">
        <h1 className="mb-2 font-mono text-sm uppercase tracking-[0.14em] text-rose-300">
          Replay unavailable
        </h1>
        <p className="text-sm leading-relaxed text-slate-400">{error}</p>
      </main>
    );
  }

  if (loading || !bundle) {
    return (
      <main className="mx-auto max-w-3xl p-8">
        <p className="font-mono text-xs text-slate-500">Loading recorded run…</p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1800px] space-y-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="font-mono text-xs uppercase tracking-[0.2em] text-slate-400">
          DevGuard · Command Center
        </h1>
        {manifest ? <RunPicker runs={manifest.runs} current={bundle.run_id} onPick={pick} /> : null}
      </div>

      <IncidentHeader bundle={bundle} />
      <PriorKnowledge bundle={bundle} />
      <HandoffRail bundle={bundle} onOpenRaw={setRawRef} />

      <div className="grid gap-2 xl:grid-cols-[22rem_1fr]">
        {/* min-w-0: a grid item defaults to min-width:auto, so this column
            refused to shrink below the ledger's min-content width (456px) and
            pushed the page into horizontal scroll on a phone. The right-hand
            column already carried it; this one did not. */}
        <div className="min-w-0 xl:sticky xl:top-2 xl:max-h-[calc(100vh-1rem)]">
          <EvidenceLedger bundle={bundle} onOpenRaw={setRawRef} />
        </div>

        <div className="min-w-0 space-y-2">
          <MetricsStrip bundle={bundle} />
          <RootCause bundle={bundle} onOpenRaw={setRawRef} />
          <BlastRadiusPanel
            radius={bundle.blast_radius}
            whyMissing={bundle.missing["blast_radius"]}
            onOpenRaw={setRawRef}
          />
          <PolicyApproval bundle={bundle} onOpenRaw={setRawRef} />
          <WriteBack bundle={bundle} onOpenRaw={setRawRef} />
          {/* Below the write-back on purpose: the catalog surface explains how
              everything above reached DataHub, so it reads as the substrate of
              the run rather than a step in it. */}
          <CatalogSurface bundle={bundle} />
          <SecurityPanel bundle={bundle} />
          <MissingLedger bundle={bundle} />
        </div>
      </div>

      <footer className="border-t border-white/10 pt-2 font-mono text-[10px] leading-relaxed text-slate-600">
        Replay of <span className="text-slate-400">{bundle.source_pack}</span> · captured{" "}
        {bundle.pack_written_at ?? "unknown"} · bundle built {bundle.built_at} ·{" "}
        {bundle.artifact_count ?? 0} artifacts embedded. Every number on this screen is read from
        that proof pack; nothing is computed live and nothing is typed by hand.{" "}
        {/* The commit this build came from. Without it, someone opening a
            deployed replay URL cannot confirm the build in front of them is
            the one that was submitted. */}
        Build{" "}
        <span className="text-slate-400">
          {(process.env.NEXT_PUBLIC_COMMIT_SHA || "").slice(0, 7) || "unknown"}
        </span>
        .
      </footer>

      <RawViewer bundle={bundle} rawRef={rawRef} onClose={() => setRawRef(null)} />
    </main>
  );
}

export default function CommandPage() {
  return (
    <Suspense
      fallback={<p className="p-8 font-mono text-xs text-slate-500">Loading recorded run…</p>}
    >
      <CommandCenter />
    </Suspense>
  );
}
