"use client";

import {
  DataSourceBadge,
  NotAvailable,
  PanelEmptyState,
  RunMeta,
  Value,
  pickArray,
  pickObject,
  pickString,
  type PanelProps,
} from "./_shared";

/* -------------------------------------------------------------------------- */
/*  ExecutiveCommanderPanel — cross-module roll-up                            */
/*                                                                            */
/*  Renders only what generate_executive_summary() actually returns: the      */
/*  composed slack_message, which sections ran, and which were omitted. The   */
/*  previous health gauge, "cost avoided" figure, phone mock and PDF preview  */
/*  had no backend counterpart and were invented, so they are gone.           */
/*                                                                            */
/*  Note: each section carries its own data_source, and they frequently       */
/*  disagree with the roll-up's own label — so per-section provenance is      */
/*  rendered explicitly rather than collapsed into one badge.                 */
/* -------------------------------------------------------------------------- */

/* ---- Shared HUD panel chrome (self-contained) ------------------------------- */

function NexusPanelStyles() {
  return (
    <style jsx global>{`
      .nx-panel {
        box-shadow: 0 0 0 1px rgba(var(--panel-glow), 0.06), 0 0 50px -18px rgba(var(--panel-glow), 0.35),
          0 20px 60px -24px rgba(0, 0, 0, 0.8), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      }
      .nx-corner {
        position: absolute;
        width: 18px;
        height: 18px;
        border-color: rgba(var(--panel-glow), 0.55);
        pointer-events: none;
      }
      .nx-corner-tl {
        top: 10px;
        left: 10px;
        border-top: 2px solid;
        border-left: 2px solid;
        border-radius: 4px 0 0 0;
      }
      .nx-corner-tr {
        top: 10px;
        right: 10px;
        border-top: 2px solid;
        border-right: 2px solid;
        border-radius: 0 4px 0 0;
      }
      .nx-corner-bl {
        bottom: 10px;
        left: 10px;
        border-bottom: 2px solid;
        border-left: 2px solid;
        border-radius: 0 0 0 4px;
      }
      .nx-corner-br {
        bottom: 10px;
        right: 10px;
        border-bottom: 2px solid;
        border-right: 2px solid;
        border-radius: 0 0 4px 0;
      }
      .nx-phone-bezel {
        box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.06), 0 20px 50px -12px rgba(0, 0, 0, 0.8);
      }
      .nx-deploy-btn {
        background: linear-gradient(180deg, #201626, #150e1c);
        box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.35), 0 6px 24px -6px rgba(244, 63, 94, 0.5);
        animation: nx-deploy-glow 2.2s ease-in-out infinite;
      }
      @keyframes nx-deploy-glow {
        0%,
        100% {
          box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.35), 0 6px 24px -6px rgba(244, 63, 94, 0.45);
        }
        50% {
          box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.55), 0 8px 28px -4px rgba(244, 63, 94, 0.7);
        }
      }
      .nx-deploy-border {
        position: absolute;
        inset: -1px;
        border-radius: 0.75rem;
        padding: 1px;
        background: conic-gradient(from var(--nx-angle, 0deg), transparent 0%, #f43f5e 25%, #6366f1 50%, transparent 75%);
        -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        animation: nx-rotate 3s linear infinite;
      }
      @property --nx-angle {
        syntax: "<angle>";
        initial-value: 0deg;
        inherits: false;
      }
      @keyframes nx-rotate {
        to {
          --nx-angle: 360deg;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .nx-deploy-btn,
        .nx-deploy-border {
          animation: none !important;
        }
      }
    `}</style>
  );
}


/* ---- Main panel --------------------------------------------------------------- */

export default function ExecutiveCommanderPanel({
  data,
  status,
  error,
  elapsedMs,
  onRunSingle,
}: PanelProps) {
  const slackMessage = pickString(data, "slack_message");
  const sections = pickObject(data, "sections");
  const omitted = pickArray(data, "omitted_sections");

  const sectionRows = sections
    ? Object.entries(sections).map(([name, payload]) => {
        const p = (payload ?? {}) as Record<string, unknown>;
        return {
          name,
          source: typeof p.data_source === "string" ? (p.data_source as string) : null,
        };
      })
    : [];

  const hasData = status === "complete" && data !== null;

  return (
    <div
      className="nx-panel relative overflow-hidden rounded-2xl border border-rose-400/25 bg-white/[0.03] p-6 backdrop-blur-xl ring-1 ring-rose-400/20 sm:p-8"
      style={{ ["--panel-glow" as string]: "251, 113, 133" }}
    >
      <NexusPanelStyles />
      <span className="nx-corner nx-corner-tl" />
      <span className="nx-corner nx-corner-tr" />
      <span className="nx-corner nx-corner-bl" />
      <span className="nx-corner nx-corner-br" />

      <div className="flex items-start justify-between gap-4">
        <div>
          <span className="rounded-md border border-rose-400/25 px-2 py-0.5 text-[10px] font-semibold tracking-widest text-rose-300">
            MOD-05
          </span>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight text-white">
            Executive Commander
          </h2>
          <p className="mt-1 text-xs font-medium uppercase tracking-wider text-rose-300">
            Cross-module roll-up
          </p>
          <RunMeta data={data} elapsedMs={elapsedMs} />
        </div>
        <DataSourceBadge data={data} status={status} />
      </div>

      {!hasData ? (
        <PanelEmptyState
          status={status}
          error={error}
          onRunSingle={onRunSingle}
          accentClass="text-rose-300"
        />
      ) : (
        <>
          {/* Per-section provenance. The roll-up's own data_source tracks whether
              anything errored, not whether the underlying data was real, so the
              sections are listed with their individual labels. */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-3 text-[10px] font-medium uppercase tracking-wider text-white/40">
              Sections included &middot; provenance per module
            </div>
            {sectionRows.length === 0 ? (
              <p className="text-xs text-white/40">
                No sections returned — <NotAvailable />
              </p>
            ) : (
              <ul className="space-y-2">
                {sectionRows.map((row) => (
                  <li
                    key={row.name}
                    className="flex items-center justify-between gap-4 rounded-lg border border-white/[0.06] px-3 py-2"
                  >
                    <span className="font-mono text-[11px] text-white/75">{row.name}</span>
                    <SourceTag source={row.source} />
                  </li>
                ))}
              </ul>
            )}

            {omitted && omitted.length > 0 && (
              <p className="mt-3 text-xs text-amber-300/85">
                Omitted this run: {omitted.map(String).join(", ")}
              </p>
            )}
          </div>

          {/* The composed summary, verbatim */}
          <div className="mt-6 rounded-xl border border-white/10 bg-black/20 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[10px] font-medium uppercase tracking-wider text-white/40">
                Composed summary
              </span>
              <span className="rounded-full border border-white/12 bg-white/[0.04] px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-white/45">
                Preview — not sent
              </span>
            </div>
            {slackMessage === null ? (
              <NotAvailable />
            ) : (
              <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-white/75">
                {slackMessage}
              </pre>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SourceTag({ source }: { source: string | null }) {
  if (source === "live") {
    return (
      <span className="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-emerald-500/15 text-emerald-300">
        Live
      </span>
    );
  }
  if (source === "local_shadow") {
    return (
      <span
        title="Measured in-process by DevGuard, not retrieved from SigNoz"
        className="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-sky-500/15 text-sky-300"
      >
        Local
      </span>
    );
  }
  if (source === "synthetic") {
    return (
      <span className="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-amber-500/15 text-amber-300">
        Simulated
      </span>
    );
  }
  return (
    <span className="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-white/10 text-white/45">
      Unlabelled
    </span>
  );
}
