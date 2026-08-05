/**
 * RawViewer.tsx — the raw payload viewer behind every evidence chip (the Command Center).
 *
 * The evidence rule's contract is that every claim points at a file a judge can open. This is
 * where that promise is kept in the UI: the content comes from the bundle's
 * embedded `raw` map, so opening a payload needs no server, no filesystem and
 * no DataHub — which is what zero-infrastructure replay's "zero infrastructure" actually requires.
 */

"use client";

import React, { useEffect } from "react";
import type { ReplayBundle } from "@/lib/replay";
import { Badge } from "./primitives";

function pretty(content: string): string {
  const trimmed = content.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return content;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    // JSONL and truncated payloads land here; showing them verbatim is right.
    return content;
  }
}

export default function RawViewer({
  bundle,
  rawRef,
  onClose,
}: {
  bundle: ReplayBundle;
  rawRef: string | null;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!rawRef) return null;
  const entry = bundle.raw[rawRef];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-4xl flex-col rounded border border-white/15 bg-[#0b0b11]"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b border-white/10 px-4 py-3">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">
              Raw payload
            </div>
            <div className="break-all font-mono text-xs text-slate-200">{rawRef}</div>
            {entry?.note ? (
              <p className="mt-1 text-[11px] italic text-slate-400">{entry.note}</p>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {entry?.truncated_in_bundle ? <Badge tone="warn">truncated</Badge> : null}
            {typeof entry?.bytes === "number" ? (
              <Badge tone="neutral">{entry.bytes} B</Badge>
            ) : null}
            <button
              onClick={onClose}
              className="rounded border border-white/15 px-2 py-1 text-xs text-slate-300 hover:bg-white/5"
            >
              Close (Esc)
            </button>
          </div>
        </header>
        <div className="overflow-auto p-4">
          {entry?.missing || entry?.content === undefined ? (
            <p className="text-xs text-rose-300">
              This artifact is listed in the pack index but its bytes are not in the bundle
              {entry?.note ? ` — ${entry.note}` : "."}
            </p>
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-300">
              {pretty(entry.content)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
