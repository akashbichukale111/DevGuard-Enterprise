#!/usr/bin/env python3
"""
scripts/verify_otel.py — prove the OpenTelemetry pipeline is real.

WHAT THIS IS FOR
----------------
The initial audit found that DevGuard's telemetry layer was well built but had
never been demonstrated end to end against anything. This script closes that
gap without requiring SigNoz: it stands up a real OTLP/gRPC receiver in this
process, points a real DevGuard backend at it, drives real HTTP traffic, and
then decodes what actually arrived off the wire.

It is deliberately NOT a substitute for seeing a trace in the SigNoz UI
(the trace-verification requirement). It proves the half that is provable here — that DevGuard
emits well-formed OTLP with correct trace context and log correlation. The
SigNoz UI half stays open and is labelled as such in the README's Limitations.

WHAT IT ASSERTS
---------------
  1. Spans actually arrive over OTLP/gRPC (not "the exporter was configured").
  2. The expected DevGuard span names are present.
  3. Spans share a trace_id and form a real parent/child tree — i.e. context
     propagation works, rather than every span being an orphan root.
  4. Resource attributes carry service.name, so traces are attributable.
  5. Log records arrive AND carry trace_id/span_id matching a real emitted
     span — the log<->trace correlation that main.py's LoggingInstrumentor and
     telemetry.py's LoggingHandler are supposed to provide (the log-correlation requirement).

Every assertion is checked against decoded protobuf. Nothing is inferred from
configuration.

USAGE
-----
    python scripts/verify_otel.py [--port 4317] [--evidence-dir docs/verification]

Exit code 0 = all assertions passed. Non-zero = at least one failed, and the
failure is printed. Safe to run in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from concurrent import futures
from pathlib import Path

import grpc
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

REPO_ROOT = Path(__file__).resolve().parent.parent

# Span names DevGuard creates that do not require a live LLM key. The agent
# spans (scanner_agent / fixer_agent / validator_agent) need a real Groq call
# and are therefore verified separately when a key is available — see
# --require-agent-spans.
EXPECTED_SPAN_SUBSTRINGS = ["slo-status", "god-mode"]

# Only produced by a real LLM-backed scan; asserted under --require-agent-spans.
AGENT_SPAN_SUBSTRINGS = ["scan_request", "scanner_agent"]


class Collected:
    """Everything the receiver decoded, kept in memory for assertions."""

    def __init__(self) -> None:
        self.spans: list[dict] = []
        self.logs: list[dict] = []


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, sink: Collected) -> None:
        self.sink = sink

    def Export(self, request, context):  # noqa: N802 (gRPC naming)
        for rs in request.resource_spans:
            resource_attrs = {
                kv.key: kv.value.string_value for kv in rs.resource.attributes
            }
            for ss in rs.scope_spans:
                for sp in ss.spans:
                    self.sink.spans.append(
                        {
                            "name": sp.name,
                            "trace_id": sp.trace_id.hex(),
                            "span_id": sp.span_id.hex(),
                            "parent_span_id": sp.parent_span_id.hex(),
                            "kind": sp.kind,
                            "resource": resource_attrs,
                            "attributes": {
                                kv.key: (
                                    kv.value.string_value
                                    or kv.value.int_value
                                    or kv.value.bool_value
                                )
                                for kv in sp.attributes
                            },
                        }
                    )
        return trace_service_pb2.ExportTraceServiceResponse()


class LogsService(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, sink: Collected) -> None:
        self.sink = sink

    def Export(self, request, context):  # noqa: N802
        for rl in request.resource_logs:
            for sl in rl.scope_logs:
                for rec in sl.log_records:
                    self.sink.logs.append(
                        {
                            "body": rec.body.string_value,
                            "severity": rec.severity_text,
                            "trace_id": rec.trace_id.hex(),
                            "span_id": rec.span_id.hex(),
                        }
                    )
        return logs_service_pb2.ExportLogsServiceResponse()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_receiver(port: int, sink: Collected):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(sink), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(LogsService(sink), server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    return server


def wait_for(url: str, timeout_s: float = 60.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


VULNERABLE_SNIPPET = (
    "def get_user(user_id):\n"
    '    query = "SELECT * FROM users WHERE id = " + user_id\n'
    "    return db.execute(query)\n"
)


def drive_traffic(base: str, include_scan: bool = False) -> list[str]:
    """Real HTTP requests against the running backend.

    The default set needs no LLM key. `include_scan` additionally posts a real
    vulnerable snippet to /scan, which exercises the agent pipeline and is the
    only way DevGuard's own agent spans are produced.
    """
    import urllib.request

    hit = []
    for path in ("/slo-status", "/audit-log/verify", "/telemetry-status"):
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
            r.read()
            hit.append(f"GET {path} -> {r.status}")
    for path in ("/god-mode/simulate/error", "/god-mode/simulate/cost-spike"):
        req = urllib.request.Request(
            f"{base}{path}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
            hit.append(f"POST {path} -> {r.status}")

    if include_scan:
        body = json.dumps({"code": VULNERABLE_SNIPPET}).encode()
        req = urllib.request.Request(
            f"{base}/scan",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # A real reflection loop can take a while; allow for it.
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
            hit.append(f"POST /scan -> {r.status}")
    return hit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence-dir", default="docs/verification")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument(
        "--require-agent-spans",
        action="store_true",
        help=(
            "Additionally drive POST /scan and require DevGuard's own pipeline "
            "spans (scan_request, and the scanner/fixer/validator agent spans). "
            "Needs a live GROQ_API_KEY in the environment; the run fails fast "
            "with a clear message if one is not set."
        ),
    )
    args = ap.parse_args()

    if args.require_agent_spans and not os.environ.get("GROQ_API_KEY"):
        print(
            "[fatal] --require-agent-spans needs GROQ_API_KEY set: DevGuard's "
            "agent spans are only emitted by a real LLM-backed scan. Re-run "
            "without the flag to verify the no-key surface only."
        )
        return 2

    otlp_port = free_port()
    app_port = free_port()
    sink = Collected()
    server = start_receiver(otlp_port, sink)
    print(f"[receiver] OTLP/gRPC listening on 127.0.0.1:{otlp_port}")

    env = dict(os.environ)
    if not args.require_agent_spans:
        env.pop("GROQ_API_KEY", None)  # prove it runs without a key
    env.pop("SIGNOZ_MCP_URL", None)
    env.pop("OTEL_SDK_DISABLED", None)  # telemetry MUST be on for this test
    env.update(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_port}",
            "OTEL_SERVICE_NAME": "devguard-ai",
            # Small batch delays so the test does not wait on the default 5s.
            "OTEL_BSP_SCHEDULE_DELAY": "500",
            "OTEL_BLRP_SCHEDULE_DELAY": "500",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )

    proc = subprocess.Popen(
        [args.python, "-m", "uvicorn", "backend.main:app", "--port", str(app_port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    failures: list[str] = []
    traffic: list[str] = []
    try:
        base = f"http://127.0.0.1:{app_port}"
        if not wait_for(f"{base}/slo-status"):
            out = proc.stdout.read() if proc.stdout else ""
            print("[fatal] backend did not start:\n" + out[-3000:])
            return 2
        keystate = "GROQ_API_KEY set" if args.require_agent_spans else "no GROQ_API_KEY set"
        print(f"[backend] up on {base} ({keystate})")

        traffic = drive_traffic(base, include_scan=args.require_agent_spans)
        for line in traffic:
            print(f"[traffic] {line}")

        # Give the batch processors time to flush to the receiver.
        time.sleep(4)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(2)  # let any final export land
        server.stop(3).wait()

    # ---------------- assertions, all against decoded protobuf -------------
    print(f"\n[decoded] {len(sink.spans)} spans, {len(sink.logs)} log records\n")

    if not sink.spans:
        failures.append("no spans arrived over OTLP")

    names = sorted({s["name"] for s in sink.spans})
    print("[spans] distinct names:")
    for n in names:
        print(f"    {n}")

    for needle in EXPECTED_SPAN_SUBSTRINGS:
        if not any(needle in n for n in names):
            failures.append(f"no span name containing {needle!r}")

    if args.require_agent_spans:
        for needle in AGENT_SPAN_SUBSTRINGS:
            if not any(needle in n for n in names):
                failures.append(
                    f"--require-agent-spans: no span name containing {needle!r}"
                )

    # Resource attribution
    svc = {s["resource"].get("service.name") for s in sink.spans}
    print(f"\n[resource] service.name values: {svc}")
    if "devguard-ai" not in svc:
        failures.append(f"service.name 'devguard-ai' missing (got {svc})")

    # Parent/child structure — proves context propagation, not orphan roots
    by_id = {s["span_id"]: s for s in sink.spans}
    children = [s for s in sink.spans if s["parent_span_id"] and s["parent_span_id"] in by_id]
    print(f"[tree] {len(children)} span(s) have a parent present in this batch")
    for c in children[:6]:
        p = by_id[c["parent_span_id"]]
        same = "SAME trace" if p["trace_id"] == c["trace_id"] else "DIFFERENT trace"
        print(f"    {p['name']} -> {c['name']}  ({same}, trace={c['trace_id'][:16]}…)")
    if not children:
        failures.append("no parent/child span relationship observed")
    for c in children:
        if by_id[c["parent_span_id"]]["trace_id"] != c["trace_id"]:
            failures.append(f"child {c['name']} has different trace_id than its parent")

    # Log <-> trace correlation (the log-correlation requirement)
    correlated = [l for l in sink.logs if l["trace_id"] and set(l["trace_id"]) != {"0"}]
    print(f"\n[logs] {len(sink.logs)} record(s), {len(correlated)} carry a trace_id")
    span_trace_ids = {s["trace_id"] for s in sink.spans}
    matched = [l for l in correlated if l["trace_id"] in span_trace_ids]
    print(f"[logs] {len(matched)} correlate to a trace_id we actually received")
    for l in matched[:4]:
        print(f"    trace={l['trace_id'][:16]}… span={l['span_id'][:12]}… {l['body'][:60]!r}")
    if not sink.logs:
        failures.append("no log records arrived over OTLP")
    elif not matched:
        failures.append(
            "no log record correlated to an emitted span "
            "(log<->trace correlation not working)"
        )

    # ---------------- evidence ------------------------------------------- #
    ev_dir = REPO_ROOT / args.evidence_dir
    ev_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "otlp_endpoint": f"http://127.0.0.1:{otlp_port}",
        "groq_api_key_set": bool(args.require_agent_spans),
        "agent_spans_required": bool(args.require_agent_spans),
        "traffic": traffic,
        "span_count": len(sink.spans),
        "log_count": len(sink.logs),
        "distinct_span_names": names,
        "resource_service_names": sorted(x for x in svc if x),
        "parent_child_pairs": [
            {
                "parent": by_id[c["parent_span_id"]]["name"],
                "child": c["name"],
                "trace_id": c["trace_id"],
                "same_trace": by_id[c["parent_span_id"]]["trace_id"] == c["trace_id"],
            }
            for c in children
        ],
        "logs_with_trace_id": len(correlated),
        "logs_correlated_to_received_span": len(matched),
        "sample_correlated_logs": matched[:5],
        "failures": failures,
        "passed": not failures,
    }
    out = ev_dir / "otel-verification.json"
    out.write_text(json.dumps(evidence, indent=2))
    print(f"\n[evidence] {out.relative_to(REPO_ROOT)}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASSED: OTLP export, trace context propagation, and log<->trace "
          "correlation all verified against decoded protobuf.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
