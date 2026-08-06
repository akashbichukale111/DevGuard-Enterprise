"use client";

// Project scanning: upload a ZIP or point at a public repository, then watch
// every supported source file go through the same Scanner -> Fixer -> Validator
// pipeline a pasted snippet does.
//
// The honesty rules this panel has to respect, because the backend reports them
// and hiding them would misrepresent a scan:
//
//   * A repository with more source files than the cap is scanned only up to
//     the cap. "25 of 121" is stated on screen — never just "25 files scanned".
//   * A file that failed to scan is not a file with no findings. Failures are
//     counted separately and shown, and a run where everything failed is
//     rendered as a failure, not as a clean result.
//   * Nothing here invents a severity, a count, or a score. Every value comes
//     from GET /scan/project/{id}.

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, FileArchive, GitBranch, Loader2 } from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/** Matches MAX_UPLOAD_BYTES in backend/api/router.py. */
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

/** How often to poll while a project scan is running. */
const POLL_INTERVAL_MS = 1200;

type FileResult = {
  path: string;
  language: string;
  status: "complete" | "failed";
  cached?: boolean;
  duration_ms: number;
  vulnerability_count: number;
  max_severity: string | null;
  error?: string;
};

type ProjectState = {
  project_id: string;
  source: string;
  status: "running" | "complete" | "complete_with_errors" | "failed";
  total: number;
  completed: number;
  files: FileResult[];
  error?: string;
  collection: {
    files_scanned: number;
    source_files_found: number;
    truncated: boolean;
    max_files: number;
    skipped_too_large: string[];
  };
  total_vulnerabilities: number;
  files_with_findings: number;
  files_analysed: number;
  files_failed: number;
};

const SEVERITY_STYLE: Record<string, string> = {
  critical: "border-rose-400/40 bg-rose-400/10 text-rose-200",
  high: "border-orange-400/40 bg-orange-400/10 text-orange-200",
  medium: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  low: "border-sky-400/40 bg-sky-400/10 text-sky-200",
};

export default function ProjectScan() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<ProjectState | null>(null);
  const [repoUrl, setRepoUrl] = useState("");
  const zipInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // Always clear the interval on unmount, otherwise navigating away mid-scan
  // leaves a timer polling a dead component.
  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback(
    (projectId: string) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const res = await fetch(`${API_BASE}/scan/project/${projectId}`);
          if (!res.ok) {
            stopPolling();
            setError("Lost track of that scan — it may have expired.");
            setBusy(false);
            return;
          }
          const next: ProjectState = await res.json();
          setState(next);
          if (next.status !== "running") {
            stopPolling();
            setBusy(false);
          }
        } catch {
          stopPolling();
          setError("Network error while following the scan.");
          setBusy(false);
        }
      }, POLL_INTERVAL_MS);
    },
    [stopPolling]
  );

  const start = useCallback(
    async (request: () => Promise<Response>) => {
      setError(null);
      setState(null);
      setBusy(true);
      try {
        const res = await request();
        const body = await res.json().catch(() => null);
        if (!res.ok) {
          setError(
            typeof body?.detail === "string"
              ? body.detail
              : `The scan was rejected (HTTP ${res.status}).`
          );
          setBusy(false);
          return;
        }
        // The POST returns the job envelope; the poll fills in the rest.
        setState({
          ...body,
          files: [],
          completed: 0,
          total_vulnerabilities: 0,
          files_with_findings: 0,
          files_analysed: 0,
          files_failed: 0,
        });
        poll(body.project_id);
      } catch {
        setError("Network error reaching the pipeline.");
        setBusy(false);
      }
    },
    [poll]
  );

  const onZip = useCallback(
    (file: File) => {
      if (file.size > MAX_UPLOAD_BYTES) {
        setError(
          `That archive is ${(file.size / (1024 * 1024)).toFixed(1)} MB. The limit is ${
            MAX_UPLOAD_BYTES / (1024 * 1024)
          } MB.`
        );
        return;
      }
      const form = new FormData();
      form.append("file", file);
      void start(() =>
        fetch(`${API_BASE}/scan/zip`, { method: "POST", body: form })
      );
    },
    [start]
  );

  const onRepo = useCallback(() => {
    if (!repoUrl.trim()) {
      setError("Paste a repository URL first.");
      return;
    }
    void start(() =>
      fetch(`${API_BASE}/scan/repository`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl.trim() }),
      })
    );
  }, [repoUrl, start]);

  return (
    <div className="space-y-5">
      {/* Inputs */}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="dg-panel rounded-2xl p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <FileArchive className="h-4 w-4 text-cyan-300" />
            Upload a ZIP
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-400">
            Every supported source file in the archive is scanned. Dependency,
            build and minified paths are skipped.
          </p>
          <input
            ref={zipInputRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onZip(f);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => zipInputRef.current?.click()}
            className="mt-4 w-full rounded-xl border border-cyan-400/25 bg-cyan-400/[0.07] px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-cyan-400/[0.14] disabled:cursor-not-allowed disabled:opacity-40"
          >
            Choose archive
          </button>
        </div>

        <div className="dg-panel rounded-2xl p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <GitBranch className="h-4 w-4 text-violet-300" />
            Scan a repository
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-400">
            Public https:// repositories on GitHub, GitLab, Bitbucket or
            Codeberg. Cloned shallow, scanned, then deleted.
          </p>
          <div className="mt-4 flex gap-2">
            <input
              type="url"
              value={repoUrl}
              disabled={busy}
              onChange={(e) => setRepoUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onRepo();
              }}
              placeholder="https://github.com/owner/repo"
              className="min-w-0 flex-1 rounded-xl border border-white/10 bg-black/40 px-3 py-2.5 font-mono text-xs text-slate-200 outline-none transition-colors placeholder:text-slate-600 focus:border-violet-400/40 disabled:opacity-40"
            />
            <button
              type="button"
              disabled={busy}
              onClick={onRepo}
              className="shrink-0 rounded-xl border border-violet-400/25 bg-violet-400/[0.07] px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-violet-400/[0.14] disabled:cursor-not-allowed disabled:opacity-40"
            >
              Scan
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2.5 rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-300" />
          <p className="text-sm text-rose-200/90">{error}</p>
        </div>
      )}

      {state && <ProjectResults state={state} />}
    </div>
  );
}

function ProjectResults({ state }: { state: ProjectState }) {
  const running = state.status === "running";
  const { collection } = state;

  return (
    <div className="dg-panel rounded-2xl">
      {/* Summary */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/5 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {running && <Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-300" />}
            <span className="truncate font-mono text-xs text-slate-400">
              {state.source}
            </span>
          </div>
          <p className="mt-1.5 text-sm text-slate-300">
            {running
              ? `Scanning ${state.completed} of ${state.total} files…`
              : `${state.files_analysed} of ${state.total} files analysed`}
            {state.files_failed > 0 && (
              <span className="text-rose-300/90">
                {" "}
                · {state.files_failed} failed
              </span>
            )}
          </p>
        </div>

        <div className="flex items-center gap-4 font-mono text-xs">
          <Stat
            label="Findings"
            value={String(state.total_vulnerabilities)}
            tone={state.total_vulnerabilities > 0 ? "warn" : "ok"}
          />
          <Stat label="Files with findings" value={String(state.files_with_findings)} />
        </div>
      </div>

      {/* The cap. Stated whenever it bit, because "25 files scanned" on a
          121-file repository would misrepresent the coverage. */}
      {collection.truncated && (
        <p className="border-b border-white/5 bg-amber-400/[0.05] px-5 py-2.5 text-xs text-amber-200/90">
          Scanned the first {collection.max_files} of{" "}
          {collection.source_files_found} source files found. The rest were not
          analysed.
        </p>
      )}

      {collection.skipped_too_large.length > 0 && (
        <p className="border-b border-white/5 px-5 py-2.5 text-xs text-slate-400">
          Skipped {collection.skipped_too_large.length} file
          {collection.skipped_too_large.length === 1 ? "" : "s"} over the
          per-file size limit.
        </p>
      )}

      {state.status === "failed" && state.error && (
        <p className="border-b border-white/5 bg-rose-400/[0.06] px-5 py-2.5 text-xs text-rose-200/90">
          {state.error}
        </p>
      )}

      {/* Per-file results */}
      {state.files.length === 0 ? (
        <p className="px-5 py-8 text-center text-sm text-slate-500">
          {running ? "Waiting for the first file to finish…" : "No results."}
        </p>
      ) : (
        <div className="max-h-[46vh] overflow-y-auto">
          <table className="w-full text-left text-sm">
            <thead className="sticky top-0 bg-[#0a0b12] text-[10.5px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-5 py-2.5 font-medium">File</th>
                <th className="px-3 py-2.5 font-medium">Language</th>
                <th className="px-3 py-2.5 font-medium">Severity</th>
                <th className="px-3 py-2.5 text-right font-medium">Findings</th>
              </tr>
            </thead>
            <tbody>
              {state.files.map((f) => (
                <tr key={f.path} className="border-t border-white/5">
                  <td className="max-w-0 px-5 py-2.5">
                    <span className="block truncate font-mono text-xs text-slate-300">
                      {f.path}
                    </span>
                    {f.status === "failed" && (
                      <span className="mt-0.5 block truncate text-[11px] text-rose-300/80">
                        {f.error ?? "Scan failed"}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-xs text-slate-400">{f.language}</td>
                  <td className="px-3 py-2.5">
                    {f.status === "failed" ? (
                      <span className="rounded-md border border-white/10 px-1.5 py-0.5 font-mono text-[10.5px] text-slate-500">
                        not analysed
                      </span>
                    ) : f.max_severity ? (
                      <span
                        className={`rounded-md border px-1.5 py-0.5 font-mono text-[10.5px] uppercase ${
                          SEVERITY_STYLE[f.max_severity] ??
                          "border-white/10 text-slate-400"
                        }`}
                      >
                        {f.max_severity}
                      </span>
                    ) : (
                      <span className="rounded-md border border-emerald-400/30 bg-emerald-400/10 px-1.5 py-0.5 font-mono text-[10.5px] uppercase text-emerald-200">
                        clean
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-xs text-slate-300">
                    {f.status === "failed" ? "—" : f.vulnerability_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "neutral";
}) {
  const color =
    tone === "warn"
      ? "text-amber-200"
      : tone === "ok"
        ? "text-emerald-200"
        : "text-slate-200";
  return (
    <div className="text-right">
      <div className={`text-base font-semibold tabular-nums ${color}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
    </div>
  );
}
