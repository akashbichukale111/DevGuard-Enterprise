// frontend/app/page.tsx
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import Editor, { type OnMount } from "@monaco-editor/react";
import { Upload } from "lucide-react";

// Side-effect import: points the Monaco loader at the self-hosted bundle in
// public/monaco/vs instead of the jsdelivr CDN. Must precede the first mount.
import "@/lib/monaco";

import { fetchWithTrace, FetchTraceError } from "@/lib/otel-frontend";
import { useScanSocket, type StatusLine } from "@/lib/useScanSocket";
import ProjectScan from "@/components/scanner/ProjectScan";

// ── Demo-ready content ───────────────────────────────────────────────────────

// Mirrors backend.core.languages.LANGUAGES, which is the registry the Scanner,
// Fixer and Validator prompts read and which the upload path maps extensions
// against. GET /languages serves the same list; this constant is the offline
// fallback so the picker still renders when the backend is unreachable. Adding
// a language means adding it in BOTH places — the backend registry is the one
// that decides what is actually supported.
const LANGUAGES = [
  { id: "python", label: "Python", ext: "py" },
  { id: "javascript", label: "JavaScript", ext: "js" },
  { id: "typescript", label: "TypeScript", ext: "ts" },
  { id: "java", label: "Java", ext: "java" },
] as const;
type LanguageId = (typeof LANGUAGES)[number]["id"];

const LANGUAGE_EXT: Record<LanguageId, string> = {
  python: "py",
  javascript: "js",
  typescript: "ts",
  java: "java",
};

// Mirrors the extension table in backend.core.languages. Used to infer the
// language of an uploaded file so the picker follows the file rather than the
// user having to set it twice.
const EXT_TO_LANGUAGE: Record<string, LanguageId> = {
  py: "python",
  pyw: "python",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  mts: "typescript",
  cts: "typescript",
  java: "java",
};

const UPLOAD_ACCEPT = Object.keys(EXT_TO_LANGUAGE)
  .map((e) => `.${e}`)
  .join(",");

// Matches ScanRequest.code's max_length. Rejecting here gives an instant, clear
// message instead of a 422 from the API after an upload round trip.
const MAX_CODE_BYTES = 50_000;

function languageForFilename(name: string): LanguageId | null {
  const ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  return EXT_TO_LANGUAGE[ext] ?? null;
}

// Realistic vulnerable snippet shown as a hint — SQLi (CWE-89) + XSS (CWE-79),
// which lines up with the live status copy so the demo tells one coherent story.
const PLACEHOLDER_SNIPPETS: Record<LanguageId, string> = {
  python: `# Paste code to scan — or run this sample (it's deliberately vulnerable)
from flask import Flask, request
import sqlite3

app = Flask(__name__)

@app.route("/user")
def get_user():
    uid = request.args.get("id")
    conn = sqlite3.connect("app.db")
    # CWE-89: user input concatenated straight into SQL
    row = conn.execute("SELECT name FROM users WHERE id = " + uid).fetchone()
    # CWE-79: reflected back to the browser without escaping
    return "<h1>Welcome " + row[0] + "</h1>"
`,
  javascript: `// Paste code to scan — or run this sample (it's deliberately vulnerable)
const express = require("express");
const db = require("./db");
const app = express();

app.get("/user", async (req, res) => {
  const id = req.query.id;
  // CWE-89: string-built SQL query
  const rows = await db.query("SELECT name FROM users WHERE id = " + id);
  // CWE-79: unescaped user data reflected into HTML
  res.send("<h1>Welcome " + rows[0].name + "</h1>");
});
`,
  typescript: `// Paste code to scan — or run this sample (it's deliberately vulnerable)
import express, { Request, Response } from "express";
import { db } from "./db";

const app = express();

app.get("/user", async (req: Request, res: Response) => {
  const id = req.query.id as string;
  // CWE-89: string-built SQL query
  const rows: any[] = await db.query("SELECT name FROM users WHERE id = " + id);
  // CWE-79: unescaped user data reflected into HTML
  res.send("<h1>Welcome " + rows[0].name + "</h1>");
});
`,
  java: `// Paste code to scan — or run this sample (it's deliberately vulnerable)
import java.sql.*;
import javax.servlet.http.*;

public class UserServlet extends HttpServlet {
  protected void doGet(HttpServletRequest req, HttpServletResponse res)
      throws Exception {
    String id = req.getParameter("id");
    Connection conn = DriverManager.getConnection("jdbc:mysql://localhost/app");
    // CWE-89: user input concatenated straight into SQL
    Statement st = conn.createStatement();
    ResultSet rs = st.executeQuery("SELECT name FROM users WHERE id = " + id);
    rs.next();
    // CWE-79: reflected back to the browser without escaping
    res.getWriter().write("<h1>Welcome " + rs.getString("name") + "</h1>");
  }
}
`,
};

const AGENT_LABEL: Record<StatusLine["agent"], string> = {
  scanner: "Scanner Agent",
  fixer: "Fixer Agent",
  validator: "Validator Agent",
  system: "DevGuard",
};

const AGENT_DOT: Record<StatusLine["agent"], string> = {
  scanner: "bg-cyan-400",
  fixer: "bg-violet-400",
  validator: "bg-emerald-400",
  system: "bg-amber-400",
};

// ── Page ─────────────────────────────────────────────────────────────────────

export default function AgentWorkspace() {
  const router = useRouter();
  const scan = useScanSocket();

  const [language, setLanguage] = useState<LanguageId>("python");
  const [code, setCode] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [exiting, setExiting] = useState(false);
  // Name of the uploaded file, so the editor tab shows what is being scanned
  // instead of a generic "untitled".
  const [filename, setFilename] = useState<string | null>(null);
  const [mode, setMode] = useState<"snippet" | "project">("snippet");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const isScanning = scan.phase === "scanning" || submitting;

  // Read a source file into the editor and follow its extension for the
  // language. Everything happens in the browser — the file is never uploaded
  // as a file; its text becomes the same `code` the editor already posts, so
  // this rides the existing /scan contract rather than adding an endpoint.
  const handleFile = useCallback(async (file: File) => {
    setSubmitError(null);

    const inferred = languageForFilename(file.name);
    if (!inferred) {
      setSubmitError(
        `Unsupported file type. DevGuard scans ${Object.values(LANGUAGES)
          .map((l) => l.label)
          .join(", ")} sources.`
      );
      return;
    }
    if (file.size > MAX_CODE_BYTES) {
      setSubmitError(
        `That file is ${(file.size / 1024).toFixed(0)} KB. The scanner accepts up to ${
          MAX_CODE_BYTES / 1000
        } KB of source at a time.`
      );
      return;
    }

    try {
      const text = await file.text();
      if (text.trim().length === 0) {
        setSubmitError("That file is empty — there is nothing to scan.");
        return;
      }
      setCode(text);
      setLanguage(inferred);
      setFilename(file.name);
    } catch {
      setSubmitError("Could not read that file.");
    }
  }, []);

  // Effective source: what the user typed, else the sample snippet (so hitting
  // "Run" on an empty editor still gives judges a working demo).
  const effectiveCode = useMemo(
    () => (code.trim().length > 0 ? code : PLACEHOLDER_SNIPPETS[language]),
    [code, language]
  );

  const handleEditorMount: OnMount = useCallback((editor, monaco) => {
    // Match Monaco's chrome to our glass panel — the default themes have a
    // solid slate background that fights the glassmorphism. A transparent
    // background lets the panel's blur show through.
    monaco.editor.defineTheme("devguard", {
      base: "vs-dark",
      inherit: true,
      rules: [],
      colors: {
        "editor.background": "#00000000",
        "editorGutter.background": "#00000000",
        "editor.lineHighlightBackground": "#ffffff08",
        "editorLineNumber.foreground": "#3f4761",
        "editorLineNumber.activeForeground": "#8b93b8",
      },
    });
    monaco.editor.setTheme("devguard");
    editor.updateOptions({ padding: { top: 18, bottom: 18 } });
  }, []);

  const handleRun = useCallback(async () => {
    if (isScanning) return;
    setSubmitError(null);
    setSubmitting(true);

    try {
      // fetchWithTrace mints the traceparent; we reuse its trace id on the WS
      // so the whole scan lives under one distributed trace.
      const { data, trace } = await fetchWithTrace<{ scan_id: string }>(
        `${process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"}/scan`,
        {
          method: "POST",
          // `language` was never sent before, so the backend always fell back
          // to its default and the picker above was decorative. It now reaches
          // the Scanner, Fixer and Validator prompts.
          body: JSON.stringify({ code: effectiveCode, language }),
        }
      );
      // Don't block on progress — the POST just hands us a scan_id, then the
      // socket drives everything.
      scan.connect(data.scan_id, trace.traceId);
    } catch (err) {
      const message =
        err instanceof FetchTraceError
          ? err.status >= 500
            ? "DevGuard pipeline is unreachable. Retry in a moment."
            : "Couldn't start the scan — check your input and try again."
          : "Network error reaching the pipeline.";
      setSubmitError(message);
    } finally {
      setSubmitting(false);
    }
  }, [effectiveCode, isScanning, language, scan]);

  // On success, play the exit animation, THEN navigate. We drive this from an
  // effect (not inline) so it fires exactly once when phase flips to success.
  // POLISH: the timeout below (600ms) is intentionally kept exactly equal to
  // the exit transition's duration (0.6s) so navigation never cuts the motion
  // short — the route change happens on the animation's last frame, not before.
  useEffect(() => {
    if (scan.phase !== "success" || !scan.finalScanId) return;
    setExiting(true);
    const EXIT_DURATION_MS = 600; // must match transition.duration below (0.6s)
    const t = setTimeout(() => {
      router.push(`/result?scan_id=${scan.finalScanId}`);
    }, EXIT_DURATION_MS);
    return () => clearTimeout(t);
  }, [scan.phase, scan.finalScanId, router]);

  return (
    <>
      <GlobalStyles />
      <main className="relative min-h-screen w-full overflow-hidden bg-[#05060a] text-slate-200 antialiased">
        {/* Ambient background: radial accent bloom + fine grid. Layered and
            pointer-events-none so they're pure atmosphere. */}
        <div className="pointer-events-none absolute inset-0 dg-grid opacity-[0.4]" />
        <div className="pointer-events-none absolute -top-40 left-1/2 h-[600px] w-[900px] -translate-x-1/2 rounded-full bg-[radial-gradient(closed,ellipse)] dg-bloom" />

        <AnimatePresence>
          {!exiting && (
            <motion.div
              key="workspace"
              // Exit: fade + slight upward drift + scale-down. Ease-out curve
              // [0.22,1,0.36,1] ("expo-out"-ish) so it decelerates hard at the
              // end — reads as the view "pulling back" into the result rather
              // than just linearly fading. Same curve is reused on the CTA's
              // micro-interactions below for a consistent motion language.
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -24, scale: 0.98 }}
              transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
              className="relative z-10 mx-auto flex min-h-screen max-w-5xl flex-col px-4 py-8 sm:px-6 lg:py-12"
            >
              <Header routing={scan.routing} />

              {/* Snippet vs project. The snippet workspace below is untouched;
                  project mode is an additional input path onto the same
                  pipeline, not a replacement for it. */}
              <div className="mt-7 flex items-center gap-1.5 self-start rounded-xl border border-white/10 bg-white/[0.03] p-1">
                {(
                  [
                    ["snippet", "Snippet"],
                    ["project", "Project"],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setMode(id)}
                    disabled={isScanning}
                    className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                      mode === id
                        ? "bg-white/[0.1] text-white"
                        : "text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {mode === "project" ? (
                <section className="mt-5 flex-1">
                  <ProjectScan />
                </section>
              ) : (
              <section className="mt-5 flex-1">
                <div className="dg-panel relative rounded-2xl">
                  {/* Toolbar */}
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-white/5 px-4 py-3 sm:px-5">
                    <div className="flex items-center gap-2">
                      <span className="h-2.5 w-2.5 rounded-full bg-rose-400/70" />
                      <span className="h-2.5 w-2.5 rounded-full bg-amber-400/70" />
                      <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/70" />
                      <span className="ml-3 truncate text-xs font-medium text-slate-400">
                        {filename ?? `untitled.${LANGUAGE_EXT[language]}`}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept={UPLOAD_ACCEPT}
                        className="hidden"
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) void handleFile(f);
                          // Reset so re-picking the same file fires onChange.
                          e.target.value = "";
                        }}
                      />
                      <button
                        type="button"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={isScanning}
                        title="Load a source file into the editor"
                        className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1 text-xs font-medium text-slate-300 transition-colors hover:bg-white/[0.09] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Upload className="h-3.5 w-3.5" />
                        Upload file
                      </button>
                      <LanguagePicker
                        value={language}
                        onChange={(l) => {
                          setLanguage(l);
                          // The filename no longer describes the buffer once
                          // the language is changed by hand.
                          setFilename(null);
                        }}
                        disabled={isScanning}
                      />
                    </div>
                  </div>

                  {/* Editor + laser overlay */}
                  <div className="relative h-[42vh] min-h-[320px] w-full overflow-hidden rounded-b-2xl">
                    <Editor
                      language={language}
                      value={code}
                      onChange={(v) => setCode(v ?? "")}
                      onMount={handleEditorMount}
                      options={{
                        minimap: { enabled: false },
                        fontSize: 14,
                        fontFamily:
                          "'JetBrains Mono','Fira Code',ui-monospace,monospace",
                        lineHeight: 22,
                        scrollBeyondLastLine: false,
                        smoothScrolling: true,
                        renderLineHighlight: "line",
                        overviewRulerLanes: 0,
                        readOnly: isScanning,
                      }}
                      loading={
                        <div className="flex h-full items-center justify-center text-sm text-slate-500">
                          Loading editor…
                        </div>
                      }
                    />

                    {/* Placeholder hint when empty and idle */}
                    {code.trim().length === 0 && !isScanning && (
                      <pre className="pointer-events-none absolute inset-0 overflow-hidden px-[62px] py-[18px] font-mono text-[14px] leading-[22px] text-slate-600/70">
                        {PLACEHOLDER_SNIPPETS[language]}
                      </pre>
                    )}

                    {/* Laser scanning sweep — only during scanning. Two layers:
                        a bright beam + a soft trailing glow, moving top→bottom. */}
                    {isScanning && (
                      <div className="pointer-events-none absolute inset-0 z-20">
                        <div className="dg-laser" />
                        <div className="absolute inset-0 dg-scan-tint" />
                      </div>
                    )}
                  </div>
                </div>

                {/* Action row */}
                <div className="mt-6 flex flex-col items-center gap-4">
                  {/* POLISH: wrapper is `relative` so the ambient glow div
                      behind the CTA can sit at z-index 0 while the button
                      itself (z-10 internally) stays crisp on top. */}
                  <div className="relative flex items-center justify-center">
                    {/* Ambient glow: a large blurred, low-opacity circle behind
                        the button. blur-2xl (~40px) + low opacity reads as
                        "light bleeding off the button" rather than a hard
                        halo — the panel-level glow uses a tighter blur since
                        it's tracing an edge, this one is meant to look diffuse. */}
                    <div
                      aria-hidden
                      className="pointer-events-none absolute h-24 w-56 rounded-full blur-2xl dg-cta-ambient"
                    />
                    <RunButton
                      onClick={handleRun}
                      scanning={isScanning}
                      disabled={isScanning}
                    />
                  </div>
                  {submitError && (
                    <p className="text-sm text-rose-300/90">{submitError}</p>
                  )}
                </div>

                {/* Circuit-breaker notice */}
                <AnimatePresence>
                  {scan.circuitBreaker && (
                    <motion.div
                      initial={{ opacity: 0, y: 8, height: 0 }}
                      animate={{ opacity: 1, y: 0, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      transition={{ duration: 0.35, ease: "easeOut" }}
                      className="mx-auto mt-6 max-w-2xl"
                    >
                      <div className="flex items-start gap-3 rounded-xl border border-amber-400/25 bg-amber-400/[0.06] px-4 py-3">
                        <span className="mt-0.5 flex h-5 w-5 flex-none items-center justify-center rounded-full bg-amber-400/20 text-amber-300">
                          ⤳
                        </span>
                        <p className="text-sm leading-relaxed text-amber-100/90">
                          Primary model unavailable — automatically routed to
                          fallback{" "}
                          <span className="font-medium text-amber-200">
                            ({scan.circuitBreaker.fallback})
                          </span>
                          . No action needed; the scan continues seamlessly.
                        </p>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                {/* Live status stream */}
                <StatusStream lines={scan.lines} score={scan.finalScore} />
              </section>
              )}

              <Footer />
            </motion.div>
          )}
        </AnimatePresence>
      </main>
    </>
  );
}

// ── Header + routing badge ───────────────────────────────────────────────────

function Header({ routing }: { routing: ReturnType<typeof useScanSocket>["routing"] }) {
  return (
    <header className="flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="dg-logo flex h-9 w-9 items-center justify-center rounded-lg text-[15px] font-bold text-white">
          ⛨
        </div>
        <div className="leading-tight">
          <h1 className="text-[15px] font-semibold tracking-tight text-white">
            DevGuard <span className="text-[color:var(--dg-accent)]">AI</span>
          </h1>
          <p className="text-[11px] text-slate-500">
            Autonomous vulnerability scan &amp; refactor
          </p>
        </div>
      </div>

      {/* Model routing indicator — subtle until it has data, then a quiet flex.
          POLISH: routed state now carries dg-badge-glow, a thin outer glow
          using --glow-accent so it visually matches the CTA/panel glow
          system instead of being a flat bordered pill. */}
      <AnimatePresence mode="wait">
        {routing ? (
          <motion.div
            key="routed"
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.3, ease: "easeOut" }}
            className="dg-badge-glow flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5"
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                routing.tier === "deep-reasoning"
                  ? "bg-violet-400"
                  : "bg-cyan-400"
              } dg-ping`}
            />
            <span className="text-[11px] font-medium text-slate-300">
              Routed to:{" "}
              <span className="text-white">
                {routing.tier === "deep-reasoning"
                  ? "Deep-Reasoning-Tier"
                  : "Fast-Tier"}
              </span>
            </span>
            <span className="text-[10px] text-slate-500">{routing.model}</span>
          </motion.div>
        ) : (
          <div
            key="idle"
            className="flex items-center gap-2 rounded-full border border-white/5 bg-white/[0.02] px-3 py-1.5"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-slate-600" />
            <span className="text-[11px] font-medium text-slate-500">
              Model routing: standby
            </span>
          </div>
        )}
      </AnimatePresence>
    </header>
  );
}

// ── Language picker ──────────────────────────────────────────────────────────

function LanguagePicker({
  value,
  onChange,
  disabled,
}: {
  value: LanguageId;
  onChange: (l: LanguageId) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1 rounded-lg bg-white/[0.03] p-0.5">
      {LANGUAGES.map((l) => (
        <button
          key={l.id}
          type="button"
          disabled={disabled}
          onClick={() => onChange(l.id)}
          className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors disabled:opacity-40 ${
            value === l.id
              ? "bg-white/[0.08] text-white"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          {l.label}
        </button>
      ))}
    </div>
  );
}

// ── Run button (glow + shimmer + press) ──────────────────────────────────────

function RunButton({
  onClick,
  scanning,
  disabled,
}: {
  onClick: () => void;
  scanning: boolean;
  disabled?: boolean;
}) {
  return (
    <motion.button
      type="button"
      onClick={onClick}
      disabled={disabled}
      // POLISH: full hover → tap → rest cycle, all on the same easing curve
      // used for page-level motion ([0.22,1,0.36,1]) so the button doesn't
      // feel like it's animating on a different "system" from the rest of
      // the page. Hover scale is deliberately small (1.02) — big enough to
      // register as "responsive" without the button jumping around next to
      // the editor. Tap goes slightly below rest (0.98) so the press reads
      // as a physical dip. Both are fast (180ms) since this is a primary CTA
      // people click repeatedly during a demo — sluggish easing here would
      // make the whole page feel laggy.
      whileHover={disabled ? undefined : { scale: 1.02 }}
      whileTap={disabled ? undefined : { scale: 0.98 }}
      transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
      className="dg-run-btn group relative z-10 inline-flex items-center gap-2.5 rounded-xl px-7 py-3.5 text-sm font-semibold text-white disabled:cursor-not-allowed"
    >
      {/* Rotating conic shimmer border sits behind the button face. */}
      <span className="dg-run-border" aria-hidden />
      <span className="relative z-10 flex items-center gap-2.5">
        {scanning ? (
          <>
            <span className="dg-spinner h-4 w-4" />
            Scanning pipeline…
          </>
        ) : (
          <>
            <span className="text-base leading-none">⚡</span>
            Run DevGuard AI Agent
          </>
        )}
      </span>
    </motion.button>
  );
}

// ── Live status stream ───────────────────────────────────────────────────────

function StatusStream({
  lines,
  score,
}: {
  lines: StatusLine[];
  score: number | null;
}) {
  if (lines.length === 0) return null;
  return (
    <div className="mx-auto mt-8 max-w-2xl">
      <div className="mb-2 flex items-center justify-between px-1">
        <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
          Live pipeline
        </span>
        {score !== null && (
          <span className="text-[11px] font-medium text-emerald-300">
            Validator score: {score}/100
          </span>
        )}
      </div>
      <div className="space-y-1.5 rounded-xl border border-white/5 bg-black/30 p-3 font-mono text-[13px]">
        <AnimatePresence initial={false}>
          {lines.map((line) => (
            <motion.div
              key={line.id}
              // Each line fades + rises in over 0.3s. Staggering is implicit
              // because lines arrive over the wire, not all at once.
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, ease: "easeOut" }}
              className="flex items-center gap-2.5"
            >
              <span
                className={`h-1.5 w-1.5 flex-none rounded-full ${AGENT_DOT[line.agent]}`}
              />
              <span className="text-slate-500">
                {AGENT_LABEL[line.agent]}
              </span>
              <span className="text-slate-200">{line.message}</span>
              {line.durationMs !== undefined && (
                <span className="ml-auto text-[11px] text-slate-600">
                  {line.durationMs}ms
                </span>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

function Footer() {
  return (
    <footer className="mt-10 flex items-center justify-center gap-4 text-[11px] text-slate-600">
      <span>W3C-traced • distributed pipeline</span>
      <span className="h-1 w-1 rounded-full bg-slate-700" />
      <span>3-agent architecture</span>
      <span className="h-1 w-1 rounded-full bg-slate-700" />
      <span>circuit-breaker protected</span>
    </footer>
  );
}

// ── Global / effect CSS ──────────────────────────────────────────────────────
// Only effects Tailwind can't express cleanly live here: the moving laser
// gradient, the conic shimmer border, the ambient bloom, and keyframes.

function GlobalStyles() {
  return (
    <style jsx global>{`
      :root {
        --dg-accent: #6366f1; /* electric indigo/violet — the product's signature */
        --dg-accent-2: #22d3ee; /* cyan secondary for scan energy */

        /* POLISH: centralized glow tokens. Everything that should "glow"
           (panel edge, CTA, badges) pulls from these two variables instead of
           hard-coding its own rgba glow color. That keeps the whole glow
           system tunable from one place and visually consistent — same hue
           family everywhere, just different intensity/blur per element. */
        --glow-primary: 99, 102, 241; /* indigo, as r,g,b so we can vary alpha per use */
        --glow-accent: 34, 211, 238; /* cyan, same pattern */
      }

      /* Fine engineering grid — reads as "infra tool", not "chatbot". */
      .dg-grid {
        background-image: linear-gradient(
            to right,
            rgba(255, 255, 255, 0.03) 1px,
            transparent 1px
          ),
          linear-gradient(to bottom, rgba(255, 255, 255, 0.03) 1px, transparent 1px);
        background-size: 44px 44px;
        mask-image: radial-gradient(ellipse 80% 60% at 50% 0%, #000 40%, transparent 100%);
      }

      /* Ambient accent bloom behind the panel. */
      .dg-bloom {
        background: radial-gradient(
          ellipse at center,
          rgba(99, 102, 241, 0.22),
          transparent 70%
        );
        filter: blur(30px);
      }

      /* Glass panel: layered translucency + inset highlight + accent-tinted
         outer glow. The inset top border sells the "lit glass edge" look.
         POLISH: box-shadow now stacks THREE layers instead of two — a tight
         1px glow ring (edge definition), a wide soft glow (the "floating"
         feel), and the original drop shadow (grounds it against the page).
         Depth reads better with a near+far glow pair than a single shadow. */
      .dg-panel {
        background: linear-gradient(
          180deg,
          rgba(255, 255, 255, 0.045),
          rgba(255, 255, 255, 0.015)
        );
        backdrop-filter: blur(14px);
        border: 1px solid rgba(255, 255, 255, 0.08);
        box-shadow: 0 0 0 1px rgba(var(--glow-primary), 0.08),
          0 0 40px -12px rgba(var(--glow-primary), 0.25),
          0 20px 60px -20px rgba(0, 0, 0, 0.8),
          inset 0 1px 0 rgba(255, 255, 255, 0.06);
      }

      .dg-logo {
        background: linear-gradient(135deg, var(--dg-accent), #8b5cf6);
        box-shadow: 0 0 20px -4px var(--dg-accent);
      }

      /* POLISH: shared glow for badges (routing pill). Kept intentionally
         subtle — 0 0 16px at low alpha — since a badge is secondary UI and
         shouldn't compete with the CTA's glow for attention. */
      .dg-badge-glow {
        box-shadow: 0 0 16px -4px rgba(var(--glow-accent), 0.35);
      }

      /* POLISH: the diffuse ambient glow div sitting behind the CTA button
         (separate from the button's own conic border/box-shadow). Radial so
         it fades to nothing at the edges rather than showing a hard blurred
         rectangle. */
      .dg-cta-ambient {
        background: radial-gradient(
          ellipse at center,
          rgba(var(--glow-primary), 0.45),
          transparent 70%
        );
      }

      /* ── Laser scan sweep ──────────────────────────────────────────────
         A bright beam translated top→bottom on a 1.8s loop. 1.8s is the sweet
         spot: fast enough to feel like active work, slow enough that the eye
         tracks a single beam rather than seeing a strobe. GPU-composited via
         transform, so it stays at 60fps over the Monaco layer. */
      .dg-laser {
        position: absolute;
        left: 0;
        right: 0;
        top: 0;
        height: 120px;
        background: linear-gradient(
          to bottom,
          transparent,
          rgba(34, 211, 238, 0.08) 40%,
          rgba(34, 211, 238, 0.55) 50%,
          rgba(34, 211, 238, 0.08) 60%,
          transparent
        );
        box-shadow: 0 0 24px rgba(34, 211, 238, 0.35);
        animation: dg-sweep 1.8s cubic-bezier(0.45, 0, 0.55, 1) infinite;
        will-change: transform;
      }
      @keyframes dg-sweep {
        0% {
          transform: translateY(-130px);
        }
        100% {
          transform: translateY(48vh);
        }
      }
      /* Faint cyan wash so the whole surface reads as "under analysis". */
      .dg-scan-tint {
        background: rgba(34, 211, 238, 0.03);
        animation: dg-pulse 1.8s ease-in-out infinite;
      }
      @keyframes dg-pulse {
        0%,
        100% {
          opacity: 0.4;
        }
        50% {
          opacity: 0.9;
        }
      }

      /* ── Run button ─────────────────────────────────────────────────────
         Face + rotating conic border. Idle glow pulses on a slow 3s cycle so
         it reads as "alive and ready" without nagging for attention.
         POLISH: now driven by --glow-primary instead of a hard-coded rgba,
         and the hover state (added below) intensifies the same glow rather
         than introducing a new color — "brighter", not "different". */
      .dg-run-btn {
        background: linear-gradient(180deg, #1a1c2e, #111220);
        box-shadow: 0 0 0 1px rgba(var(--glow-primary), 0.3),
          0 8px 30px -8px rgba(var(--glow-primary), 0.5);
        animation: dg-idle-glow 3s ease-in-out infinite;
        overflow: hidden;
        transition: box-shadow 0.18s ease-out;
      }
      .dg-run-btn:hover {
        box-shadow: 0 0 0 1px rgba(var(--glow-primary), 0.65),
          0 10px 44px -6px rgba(var(--glow-primary), 0.8);
      }
      @keyframes dg-idle-glow {
        0%,
        100% {
          box-shadow: 0 0 0 1px rgba(var(--glow-primary), 0.3),
            0 8px 30px -8px rgba(var(--glow-primary), 0.4);
        }
        50% {
          box-shadow: 0 0 0 1px rgba(var(--glow-primary), 0.5),
            0 8px 38px -6px rgba(var(--glow-primary), 0.65);
        }
      }
      /* Conic shimmer ring rotating behind the face (Aceternity pattern). */
      .dg-run-border {
        position: absolute;
        inset: -1px;
        border-radius: 0.75rem;
        padding: 1px;
        background: conic-gradient(
          from var(--dg-angle, 0deg),
          transparent 0%,
          var(--dg-accent-2) 20%,
          var(--dg-accent) 40%,
          transparent 60%
        );
        -webkit-mask: linear-gradient(#000 0 0) content-box,
          linear-gradient(#000 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        animation: dg-rotate 4s linear infinite;
      }
      @property --dg-angle {
        syntax: "<angle>";
        initial-value: 0deg;
        inherits: false;
      }
      @keyframes dg-rotate {
        to {
          --dg-angle: 360deg;
        }
      }

      /* Spinner + routing ping. */
      .dg-spinner {
        border: 2px solid rgba(255, 255, 255, 0.25);
        border-top-color: #fff;
        border-radius: 9999px;
        animation: dg-spin 0.7s linear infinite;
      }
      @keyframes dg-spin {
        to {
          transform: rotate(360deg);
        }
      }
      .dg-ping {
        animation: dg-ping 1.6s ease-in-out infinite;
      }
      @keyframes dg-ping {
        0%,
        100% {
          opacity: 1;
          transform: scale(1);
        }
        50% {
          opacity: 0.5;
          transform: scale(0.75);
        }
      }

      /* Respect reduced-motion — kill the looping animations for a11y. */
      @media (prefers-reduced-motion: reduce) {
        .dg-laser,
        .dg-scan-tint,
        .dg-run-btn,
        .dg-run-border,
        .dg-ping {
          animation: none !important;
        }
      }
    `}</style>
  );
}
