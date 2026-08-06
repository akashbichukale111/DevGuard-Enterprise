/**
 * BlastRadius.tsx — the blast-radius panel: the graph and blast-radius pane.
 *
 * The two traces are shown separately rather than merged, because the
 * integration finding 14 established that the live graph will not give you both
 * at once: the column-level trace follows `schemaField` edges and stops at the
 * last dataset, while the dataset-level trace reaches the ML model but says
 * nothing about which column carried the damage. Merging them in the UI would
 * invent an edge the graph never returned.
 *
 * The blast-radius panel also asks for impact "ranked by real downstream consumption where
 * available — never by raw edge count alone", which is why the queries list
 * sits directly beneath the graph: one real SQL query referencing the dead
 * column outranks any number of edges.
 */

"use client";

import React from "react";
import {
  entityKind,
  platformOf,
  shortUrn,
  type BlastRadius as BR,
  type ImpactedEntity,
} from "@/lib/replay";
import { Badge, Empty, NA, Panel, Urn } from "./primitives";

function EntityRow({ e, column }: { e: ImpactedEntity; column: string | null }) {
  return (
    <li
      className={`flex items-center gap-2 rounded border px-2 py-1 ${
        e.is_ml_model
          ? "border-amber-400/50 bg-amber-400/[0.08]"
          : "border-emerald-500/25 bg-emerald-500/[0.04]"
      }`}
      title={e.urn}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          e.is_ml_model ? "bg-amber-300" : "bg-emerald-400"
        }`}
      />
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-200">
        {shortUrn(e.urn)}
        {platformOf(e.urn) ? (
          <span className="ml-1.5 text-[10px] text-slate-500">@{platformOf(e.urn)}</span>
        ) : null}
      </span>
      <Badge tone={e.is_ml_model ? "warn" : "neutral"}>{entityKind(e.urn)}</Badge>
      <span className="w-12 shrink-0 text-right font-mono text-[10px] text-slate-500">
        {e.hops === null ? "N/A" : `${e.hops} hop${e.hops === 1 ? "" : "s"}`}
      </span>
    </li>
  );
}

export default function BlastRadiusPanel({
  radius,
  whyMissing,
  onOpenRaw,
}: {
  radius: BR | null;
  whyMissing?: string;
  onOpenRaw: (ref: string) => void;
}) {
  if (!radius) {
    return (
      <Panel title="Graph & blast radius">
        <Empty>{whyMissing ?? "No lineage was traced in this run."}</Empty>
      </Panel>
    );
  }

  const ml = radius.terminal_ml_models;

  return (
    <Panel
      title="Graph & blast radius"
      subtitle={
        <>
          from <span className="font-mono text-slate-400">{shortUrn(radius.root_urn)}</span>
          {radius.column ? (
            <>
              {" "}
              · column <span className="font-mono text-slate-300">{radius.column}</span>
            </>
          ) : null}
        </>
      }
      right={
        radius.max_hops === null ? (
          <NA why="No hop count was returned by the graph." />
        ) : (
          `max ${radius.max_hops} hops`
        )
      }
      tone={ml.length > 0 ? "alert" : "default"}
    >
      {/* min-w-0 on both columns: grid items default to min-width:auto, which
          makes a column refuse to shrink below its content and pushes the page
          sideways on a phone. The URN rows inside already truncate. */}
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="min-w-0">
          <h3 className="mb-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Column-level trace · {radius.column_level.length} entities
            <span
              className="ml-1 cursor-help text-slate-600"
              title="Follows schemaField edges. Precise — every hit carries the column — but it stops at the last dataset, because the ML model's edge is not a column edge."
            >
              ⓘ
            </span>
          </h3>
          {radius.column_level.length === 0 ? (
            <Empty>No column-level trace was captured.</Empty>
          ) : (
            <ul className="space-y-1">
              {radius.column_level.map((e) => (
                <EntityRow key={`c-${e.urn}`} e={e} column={radius.column} />
              ))}
            </ul>
          )}
        </div>

        <div className="min-w-0">
          <h3 className="mb-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Dataset-level trace · {radius.dataset_level.length} entities
            <span
              className="ml-1 cursor-help text-slate-600"
              title="Follows dataset → dataJob → mlModel. Reaches the terminus, but says nothing about which column carried the damage."
            >
              ⓘ
            </span>
          </h3>
          {radius.dataset_level.length === 0 ? (
            <Empty>No dataset-level trace was captured.</Empty>
          ) : (
            <ul className="space-y-1">
              {radius.dataset_level.map((e) => (
                <EntityRow key={`d-${e.urn}`} e={e} column={radius.column} />
              ))}
            </ul>
          )}
        </div>
      </div>

      {ml.length > 0 ? (
        <div className="mt-3 rounded border border-amber-400/50 bg-amber-400/[0.08] px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-[0.12em] text-amber-300/80">
            Terminal ML model
          </div>
          {ml.map((m) => (
            <Urn key={m.urn} value={m.urn} className="!text-amber-100" />
          ))}
        </div>
      ) : null}

      {radius.column_path.length > 0 ? (
        <div className="mt-3">
          <h3 className="mb-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Column path · {radius.column_path.length} hops
          </h3>
          <ol className="flex flex-wrap items-center gap-1">
            {radius.column_path.map((urn, i) => (
              <React.Fragment key={`${urn}-${i}`}>
                {i > 0 ? <span className="text-slate-700">→</span> : null}
                <li
                  className="rounded border border-white/10 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-slate-300"
                  title={urn}
                >
                  {shortUrn(urn)}
                  {platformOf(urn) ? (
                    <span className="ml-1 text-slate-500">@{platformOf(urn)}</span>
                  ) : null}
                </li>
              </React.Fragment>
            ))}
          </ol>
        </div>
      ) : null}

      <div className="mt-3 border-t border-white/10 pt-2">
        <h3 className="mb-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
          Real queries referencing this column · {radius.queries.length}
          <span
            className="ml-1 cursor-help text-slate-600"
            title="Impact ranked by real downstream consumption, not by edge count alone."
          >
            ⓘ
          </span>
        </h3>
        {radius.queries.length === 0 ? (
          <Empty>
            The catalog returned no queries referencing this column. That is the graph&apos;s
            answer, not a missing panel.
          </Empty>
        ) : (
          <ul className="space-y-1">
            {radius.queries.map((q) => (
              <li key={q.urn} className="rounded border border-white/10 bg-black/20 p-2">
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone="info">{q.source}</Badge>
                  <span className="truncate font-mono text-[9px] text-slate-600" title={q.urn}>
                    {q.urn}
                  </span>
                  {radius.queries_raw_ref ? (
                    <button
                      onClick={() => onOpenRaw(radius.queries_raw_ref as string)}
                      className="ml-auto shrink-0 font-mono text-[10px] text-sky-400 underline decoration-sky-500/40 underline-offset-2 hover:text-sky-300"
                    >
                      raw
                    </button>
                  ) : null}
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[10px] leading-snug text-slate-300">
                  {q.statement}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}
