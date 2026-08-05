#!/usr/bin/env python3
"""
scripts/doctor.py — preflight diagnosis.

    "A judge who hits a wall gets a diagnosis, not a stack trace."

Checks Python and Node versions, required and optional environment variables,
whether the backend imports, whether its dependencies are present, and whether
the backend/frontend are reachable — then prints exactly what is missing and
what to do about it.

Design rules this follows, learned from the initial audit:
  * Every check reports what it actually observed. Nothing is inferred from
    configuration, and nothing is reported as OK because it "should" be.
  * Optional things are labelled OPTIONAL and never fail the run. DevGuard is
    designed to work without Redis, without a collector and without SigNoz;
    a doctor that reddened on those would be lying about the requirements.
  * Exit code is 0 only when every REQUIRED check passes.

Usage:
    python scripts/doctor.py            # or: make doctor
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GREEN, AMBER, RED, GREY, BOLD, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = AMBER = RED = GREY = BOLD = RESET = ""

MIN_PYTHON = (3, 10)
# Raised 18 -> 20 by the approved next@14 -> next@16 upgrade. This is not a
# preference: `next@16.2.12` declares `engines = { node: ">=20.9.0" }`, so on Node
# 18 `next build` refuses to run. Leaving it at 18 would have made doctor report
# OK on a machine where the frontend cannot be built, which is the one thing this
# script exists to prevent. The minor floor is checked too, because 20.0-20.8 are
# also below Next's range.
MIN_NODE = (20, 9)
MIN_NODE_MAJOR = MIN_NODE[0]

results: list[tuple[str, str, str, str]] = []  # (status, name, detail, remedy)


def ok(name: str, detail: str) -> None:
    results.append(("OK", name, detail, ""))


def warn(name: str, detail: str, remedy: str = "") -> None:
    results.append(("OPTIONAL", name, detail, remedy))


def fail(name: str, detail: str, remedy: str) -> None:
    results.append(("MISSING", name, detail, remedy))


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_python() -> None:
    v = sys.version_info
    got = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        ok("Python", f"{got} (>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")
    else:
        fail(
            "Python",
            f"{got} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and recreate the venv.",
        )


def check_node() -> None:
    floor = f"{MIN_NODE[0]}.{MIN_NODE[1]}"
    node = shutil.which("node")
    if not node:
        warn(
            "Node.js",
            "not found on PATH",
            "Only needed for the frontend. Install Node "
            f"{floor}+ if you want to run the UI.",
        )
        return
    try:
        raw = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        parts = raw.lstrip("v").split(".")
        got = (int(parts[0]), int(parts[1]))
        if got >= MIN_NODE:
            ok("Node.js", f"{raw} (>= {floor})")
        else:
            warn("Node.js", f"{raw} is older than {floor}",
                 f"Upgrade to Node {floor}+ — next@16 will not build below it.")
    except Exception as exc:  # noqa: BLE001
        warn("Node.js", f"present but not queryable ({exc})", "")


def check_backend_dependencies() -> None:
    """Import the packages backend/main.py needs, and name the missing one.

    These two were absent from requirements.txt at one point and made the backend
    unimportable, so they are checked explicitly rather than assumed.
    """
    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn[standard]",
        "pydantic": "pydantic",
        "httpx": "httpx",
        "groq": "groq",
        "opentelemetry.sdk.trace": "opentelemetry-sdk",
        "opentelemetry.instrumentation.fastapi": "opentelemetry-instrumentation-fastapi",
        "opentelemetry.instrumentation.logging": "opentelemetry-instrumentation-logging",
    }
    missing = []
    for module, package in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        fail(
            "Backend dependencies",
            f"{len(missing)} missing: {', '.join(missing)}",
            "pip install -r requirements.txt",
        )
    else:
        ok("Backend dependencies", f"all {len(required)} importable")


def check_backend_imports() -> None:
    """The real gate: does `import backend.main` actually work?

    Deliberately checked WITHOUT a GROQ_API_KEY. This once raised at
    import time, so a judge could not even start the server without a paid key.
    """
    env = dict(os.environ)
    env.pop("GROQ_API_KEY", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", "import backend.main"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120,
    )
    if proc.returncode == 0:
        ok("Backend imports", "`import backend.main` succeeds with no API key set")
    else:
        tail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        fail("Backend imports", tail, "Run `pip install -r requirements.txt`.")


def check_env_vars() -> None:
    if os.environ.get("GROQ_API_KEY"):
        ok("GROQ_API_KEY", "set — live scans available")
    else:
        warn(
            "GROQ_API_KEY",
            "not set — the server runs, but POST /scan cannot call an LLM",
            "cp .env.example .env and add your key from https://console.groq.com/keys",
        )

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        ok("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    else:
        warn("OTEL_EXPORTER_OTLP_ENDPOINT",
             "not set — defaults to http://localhost:4317",
             "Telemetry is fail-safe; nothing breaks if no collector is running.")

    if os.environ.get("SIGNOZ_MCP_URL"):
        warn("SIGNOZ_MCP_URL", "set — note this integration is UNVERIFIED",
             "See docs/MCP_DECISION.md before trusting its output.")
    else:
        warn("SIGNOZ_MCP_URL",
             "not set — self-observation uses the in-process shadow",
             "Expected. Reported as data_source 'local_shadow', never 'live'.")


def check_files() -> None:
    for rel, remedy in [
        (".env.example", "Should be committed; re-pull the repo."),
        ("requirements.txt", "Should be committed; re-pull the repo."),
        ("otel-collector-config.yaml", "Needed by `docker compose --profile obs up`."),
    ]:
        if (REPO_ROOT / rel).exists():
            ok(f"File {rel}", "present")
        else:
            fail(f"File {rel}", "missing", remedy)

    if (REPO_ROOT / "frontend" / "node_modules").exists():
        ok("Frontend deps", "node_modules present")
    else:
        warn("Frontend deps", "node_modules missing",
             "cd frontend && npm install")

    if (REPO_ROOT / "LICENSE").exists():
        ok("LICENSE", "present")
    else:
        warn("LICENSE", "no LICENSE file yet",
             "Tracked as an open decision in docs/MCP_DECISION.md.")


def check_rag_backend() -> None:
    """Report which retrieval backend is ACTUALLY active, not which is pinned.

    requirements.txt pins chromadb and sentence-transformers as the preferred
    backends, but both currently fail to import against the versions pip
    resolves (see SECURITY.md). The store
    then silently falls back to pure Python. "Silently" is the problem: a
    reviewer would reasonably assume the 5.4 GiB of torch on disk is being
    used. This surfaces the truth.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.core.rag_store import get_store;"
            "s = get_store();"
            "print(s.backend + '|' + s._embedder.name)",
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=180,
    )
    if proc.returncode != 0:
        fail(
            "RAG store",
            (proc.stderr.strip().splitlines() or ["failed to build"])[-1],
            "The Scanner cannot retrieve CWE context. Run `pip install -r requirements.txt`.",
        )
        return

    backend, _, embedder = proc.stdout.strip().partition("|")
    if "NOT-PROD" in embedder:
        warn(
            "RAG backend",
            f"{backend} / {embedder}",
            "Pure-Python fallback is active — the pinned chromadb and "
            "sentence-transformers backends are not importable. Deterministic "
            "and relevant, but not semantic embeddings. See "
            "SECURITY.md",
        )
    else:
        ok("RAG backend", f"{backend} / {embedder}")


def check_reachability() -> None:
    import urllib.error
    import urllib.request

    for name, url in [
        ("Backend", f"http://localhost:{os.environ.get('PORT', '8000')}/slo-status"),
        ("Frontend", "http://localhost:3000"),
    ]:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                ok(f"{name} reachable", f"{url} -> HTTP {r.status}")
        except urllib.error.HTTPError as e:
            ok(f"{name} reachable", f"{url} -> HTTP {e.code}")
        except Exception:  # noqa: BLE001
            warn(f"{name} reachable", f"nothing listening on {url}",
                 "Start it, or ignore this if you only wanted a static check.")


# --------------------------------------------------------------------------- #

def main() -> int:
    print(f"\n{BOLD}DevGuard doctor{RESET}  {GREY}preflight diagnosis{RESET}\n")

    check_python()
    check_node()
    check_backend_dependencies()
    check_backend_imports()
    check_env_vars()
    check_files()
    check_rag_backend()
    check_reachability()

    width = max(len(n) for _, n, _, _ in results) + 2
    for status, name, detail, _ in results:
        colour = {"OK": GREEN, "OPTIONAL": AMBER, "MISSING": RED}[status]
        mark = {"OK": "OK      ", "OPTIONAL": "OPTIONAL", "MISSING": "MISSING "}[status]
        print(f"  {colour}{mark}{RESET}  {name:<{width}} {GREY}{detail}{RESET}")

    missing = [r for r in results if r[0] == "MISSING"]
    optional = [r for r in results if r[0] == "OPTIONAL" and r[3]]

    if optional:
        print(f"\n{AMBER}Optional — not required to run DevGuard:{RESET}")
        for _, name, _, remedy in optional:
            print(f"  {name}: {remedy}")

    if missing:
        print(f"\n{RED}{BOLD}Required, and missing:{RESET}")
        for _, name, detail, remedy in missing:
            print(f"  {RED}{name}{RESET}: {detail}\n    -> {remedy}")
        print(f"\n{RED}doctor: {len(missing)} required check(s) failed.{RESET}\n")
        return 1

    print(f"\n{GREEN}doctor: all required checks passed.{RESET}")
    print(f"{GREY}Start the backend:  python -m uvicorn backend.main:app --port 8000{RESET}")
    print(f"{GREY}Start the frontend: cd frontend && npm run dev{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
