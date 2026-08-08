/**
 * CatalogSurface.tsx — which DataHub tools this run was offered, and which it used.
 *
 * Capability negotiation is one of this system's load-bearing design choices,
 * and it was invisible. The tool list is what the server offered **on the day**,
 * not a constant compiled into the client: a catalog with no documents does not
 * advertise `search_documents` or `grep_documents`, so a first run against a
 * clean instance negotiates six tools and the Archivist correctly reports
 * DEGRADED. After that run writes a runbook, the next one negotiates eight and
 * retrieves it.
 *
 * Two numbers on this panel are therefore the loop closing — and, unlike a
 * sentence claiming it closed, they come out of the proof pack.
 *
 * `used` counts real invocations from the handoff records. A tool that was
 * offered and never called is shown dimmed rather than hidden: the gap between
 * "available" and "needed" is information about the agents, and hiding it would
 * make the panel look like a feature list.
 */

"use client";

import React from "react";
import type { ReplayBundle } from "@/lib/replay";
import { Badge, Empty, Panel } from "./primitives";

/** What each tool is for, so the panel reads as engineering rather than a list of names. */
const PURPOSE: Record<string, string> = {
  search: "Entity resolution — a string in a log becomes a real URN",
  get_entities: "Reads an entity: owners, properties, ML model detail",
  list_schema_fields: "What the catalog believes the columns are",
  get_lineage: "Blast radius, paginated and cycle-safe",
  get_lineage_paths_between: "How A reaches B — the query entity in the middle",
  get_dataset_queries: "Which SQL actually touches the failing column",
  search_documents: "Prior incidents — discovery. Absent on a catalog with no documents",
  grep_documents: "Prior incidents — content for the URNs discovery found",
  add_tags: "Write-back artifact 3 (Scribe only)",
  update_description: "Write-back artifact 3 (Scribe only)",
  save_document: "Write-back artifact 2 — the runbook the next run retrieves",
  add_structured_properties: "Write-back artifact 4 — typed incident facts",
  add_owners: "Write-back artifact 5 — ownership signal",
};

export default function CatalogSurface({ bundle }: { bundle: ReplayBundle }) {
  const catalog = bundle.catalog;

  // A pack whose Archivist never ran has no negotiated set. That is a real
  // state — the refusal run is one — and it says so rather than rendering an
  // empty list that reads like a server offering nothing.
  if (!catalog || !catalog.capabilities_ref) {
    return (
      <Panel
        title="Catalog surface"
        subtitle="The DataHub tools this run negotiated over MCP"
        right={<Badge tone="neutral">not negotiated</Badge>}
      >
        <Empty>
          No capability set was captured for this run — the Archivist, which
          performs the handshake, did not run. Nothing is inferred about what the
          server would have offered.
        </Empty>
      </Panel>
    );
  }

  const used = catalog.used ?? {};
  const usedCount = Object.keys(used).length;
  const totalCalls = Object.values(used).reduce((a, b) => a + b, 0);
  const offered = catalog.offered ?? [];

  return (
    <Panel
      title="Catalog surface"
      subtitle={
        <>
          Negotiated live over MCP with{" "}
          <span className="font-mono text-slate-400">
            {catalog.server?.name ?? "unknown"} {catalog.server?.version ?? ""}
          </span>
          {catalog.protocol_version ? (
            <> · protocol {catalog.protocol_version}</>
          ) : null}
          . The tool list is the server&apos;s answer for this run, not a constant.
        </>
      }
      right={
        <span className="font-mono">
          {usedCount}/{catalog.offered_count ?? offered.length} used · {totalCalls} calls
        </span>
      }
    >
      <ul className="space-y-1">
        {offered.map((tool) => {
          const calls = used[tool] ?? 0;
          return (
            <li
              key={tool}
              className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b border-white/5 pb-1 last:border-0"
            >
              <span
                className={`font-mono text-xs ${calls ? "text-slate-200" : "text-slate-600"}`}
              >
                {tool}
              </span>
              {calls ? (
                <Badge tone="good" title={`${calls} call(s) in this run`}>
                  {calls}×
                </Badge>
              ) : (
                <Badge tone="neutral" title="Offered by the server, not needed by this run">
                  unused
                </Badge>
              )}
              <span className="min-w-0 flex-1 text-[11px] leading-tight text-slate-500">
                {PURPOSE[tool] ?? "No description recorded for this tool."}
              </span>
            </li>
          );
        })}
      </ul>

      {/* Tools an agent called that the server never advertised would be a
          contract violation worth seeing immediately. There should never be
          any; if there are, the allowlist and the negotiated set disagree. */}
      {Object.keys(used).filter((t) => !offered.includes(t)).length > 0 ? (
        <p className="mt-2 rounded border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
          Called but not offered:{" "}
          <span className="font-mono">
            {Object.keys(used)
              .filter((t) => !offered.includes(t))
              .join(", ")}
          </span>
          . The negotiated set and the calls disagree.
        </p>
      ) : null}

      <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
        Read from{" "}
        <span className="font-mono text-slate-500">{catalog.capabilities_ref}</span>. The
        server version above is reported verbatim and does not match the MCP package
        version on PyPI — that disagreement is the server&apos;s, and is recorded rather
        than normalised.
      </p>
    </Panel>
  );
}
