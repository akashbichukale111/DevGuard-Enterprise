#!/usr/bin/env bash
# Verify the observability requirement and the trace-verification requirement from a COMPLETELY CLEAN stack.
#
# Tears the SigNoz stack down (including volumes), brings it back up, drives real
# DevGuard traffic through it, and asserts that spans are actually STORED in
# SigNoz's ClickHouse and readable back. Exits non-zero on any failed assertion,
# so "it worked" is a green exit code and not a judgement call.
#
#   ./scripts/verify_signoz.sh
#
# Requires: a running Docker daemon and the repo's Python venv (or any python
# with the backend's dependencies). No GROQ_API_KEY is needed — see the note on
# agent spans at the bottom of this file.

set -uo pipefail

COMPOSE="signoz/deploy/docker-compose.yaml"
UI="http://127.0.0.1:3301"
OTLP_HTTP="http://127.0.0.1:4318"
BACKEND="http://127.0.0.1:8000"
CH="signoz-signoz-telemetrystore-clickhouse-0-0-1"
EMAIL="${SIGNOZ_EMAIL:-devguard@example.com}"
PASSWORD="${SIGNOZ_PASSWORD:-DevGuard!2026}"
PYTHON="${PYTHON:-python}"

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }
q()    { docker exec "$CH" clickhouse-client -q "$1" 2>/dev/null; }

step "0. Tear down completely (including volumes)"
docker compose -f "$COMPOSE" down -v >/dev/null 2>&1
docker ps -a --format '{{.Names}}' | grep -q '^signoz-' && fail "containers survived teardown"
echo "clean"

step "1. Bring up everything EXCEPT the ingester"
# Ordering is deliberate and load-bearing. The signoz-otel-collector fetches its
# effective pipeline config from the SigNoz server over OpAMP, and the server
# refuses to register an agent before an organisation exists:
#     "failed to find or create agent ... cannot create agent without orgId"
# When that happens the collector starts, reports healthy, and NEVER OPENS ITS
# OTLP RECEIVER — so every span is silently dropped. Creating the org first
# makes this deterministic instead of depending on a 30s retry.
docker compose -f "$COMPOSE" up -d signoz-signoz-0 >/dev/null 2>&1 \
  || fail "compose up (core) failed"

for i in $(seq 1 90); do
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 "$UI/api/v1/health" || true)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || fail "SigNoz API never became healthy"
echo "SigNoz API healthy"

step "2. Confirm the schema migrator ran"
migrator_rc=$(docker inspect -f '{{.State.ExitCode}}' signoz-signoz-schema-migrator-sync-1 2>/dev/null || echo missing)
[ "$migrator_rc" = "0" ] || fail "schema migrator did not exit 0 (got: $migrator_rc)"
q "EXISTS DATABASE signoz_traces" | grep -q 1 || fail "signoz_traces database missing"
echo "migrator exit 0; signoz_traces exists"

step "3. First-run setup: create the organisation"
reg=$(curl -s --noproxy '*' --max-time 30 -X POST "$UI/api/v1/register" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"DevGuard\",\"orgName\":\"devguard\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
echo "$reg" | grep -q '"orgId"' || echo "  (register returned no orgId — fine if the org already exists)"
curl -s --noproxy '*' --max-time 15 "$UI/api/v1/version"; echo

step "4. Start the ingester (now that an org exists)"
docker compose -f "$COMPOSE" up -d >/dev/null 2>&1 || fail "compose up (full) failed"
for i in $(seq 1 60); do
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 \
         -X POST "$OTLP_HTTP/v1/traces" -H 'Content-Type: application/json' -d '{}' || true)
  [ "$code" = "200" ] && break
  sleep 3
done
[ "$code" = "200" ] || fail "OTLP receiver never opened — the collector never got its OpAMP config"
echo "OTLP receiver accepting on 4318"

step "5. Start the DevGuard backend, exporting to the live ingester"
# no_grpc_proxy matters: gRPC reads its own proxy variables, and this session's
# HTTPS_PROXY would otherwise swallow a loopback OTLP connection. NO_PROXY alone
# does NOT cover gRPC.
pkill -f "uvicorn backend.main" >/dev/null 2>&1 || true
sleep 2
no_grpc_proxy='localhost,127.0.0.1' NO_PROXY='*' no_proxy='*' \
HTTPS_PROXY= https_proxy= \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
OTEL_SERVICE_NAME=devguard-backend \
nohup "$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 \
  > /tmp/devguard-verify-signoz.log 2>&1 &
for i in $(seq 1 40); do
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 "$BACKEND/telemetry-status" || true)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || fail "DevGuard backend did not start"
echo "backend up"

step "6. Drive real traffic"
for p in /telemetry-status /slo-status /audit-log /audit-log/verify; do
  curl -s --noproxy '*' -o /dev/null --max-time 30 "$BACKEND$p"
done
scan=$(curl -s --noproxy '*' --max-time 180 -X POST "$BACKEND/scan" \
  -H 'Content-Type: application/json' \
  -d '{"code":"import sqlite3\ndef get_user(uid):\n    conn = sqlite3.connect(\"app.db\")\n    return conn.execute(\"SELECT * FROM users WHERE id = \" + uid).fetchall()\n","language":"python"}')
TRACE_ID=$(printf '%s' "$scan" | "$PYTHON" -c 'import sys,json
d=json.load(sys.stdin); print(d.get("detail",{}).get("trace_id") or d.get("trace_id") or "")' 2>/dev/null)
[ -n "$TRACE_ID" ] || fail "no trace_id returned by /scan"
echo "trace_id: $TRACE_ID"

step "7. ASSERT: spans are STORED in SigNoz"
# Wait on the ROOT span, not on a bare count. Spans flush through two batching
# layers (the SDK's BatchSpanProcessor, then the collector's batch processor),
# and a parent span is only exported once it ENDS — so `scan_request` is the
# LAST span of the trace to arrive, not the first. Polling for "any span" and
# then asserting immediately is a race that reports a half-written trace as a
# failure; this loop waits for the piece that lands last.
for i in $(seq 1 60); do
  root=$(q "SELECT count() FROM signoz_traces.distributed_signoz_index_v3
            WHERE trace_id='$TRACE_ID' AND name='scan_request'")
  [ "${root:-0}" -gt 0 ] 2>/dev/null && break
  sleep 3
done
[ "${root:-0}" -gt 0 ] 2>/dev/null || fail "root span 'scan_request' never arrived for trace $TRACE_ID"

n=$(q "SELECT count() FROM signoz_traces.distributed_signoz_index_v3 WHERE serviceName='devguard-backend'")
echo "devguard-backend spans stored: $n"

trace_spans=$(q "SELECT count() FROM signoz_traces.distributed_signoz_index_v3 WHERE trace_id='$TRACE_ID'")
[ "${trace_spans:-0}" -gt 0 ] 2>/dev/null || fail "the /scan trace itself is not in SigNoz"
echo "spans in trace $TRACE_ID: $trace_spans"

echo
echo "--- span hierarchy as SigNoz stored it ---"
q "SELECT name, statusCodeString, spanID, parentSpanID
   FROM signoz_traces.distributed_signoz_index_v3
   WHERE trace_id='$TRACE_ID' ORDER BY timestamp FORMAT TSV"

step "8. ASSERT: the expected pipeline spans are present"
names=$(q "SELECT DISTINCT name FROM signoz_traces.distributed_signoz_index_v3 WHERE trace_id='$TRACE_ID'")
for expected in scan_request resilient_pipeline devguard_pipeline scanner_agent; do
  printf '%s\n' "$names" | grep -qx "$expected" || fail "expected span '$expected' missing from the trace"
  echo "  present: $expected"
done

echo
echo "================================================================"
echo "PASSED — SigNoz is up (the observability requirement) and a real DevGuard trace is stored"
echo "and readable back out of it (the trace-verification requirement)."
echo "  UI:       $UI"
echo "  trace:    $UI/trace/$TRACE_ID"
echo "================================================================"
echo
echo "NOT asserted here, and deliberately so: fixer_agent and validator_agent."
echo "Without a live GROQ_API_KEY the Scanner's LLM call fails, so the pipeline"
echo "never reaches the Fixer. The trace above is real and complete for what the"
echo "code actually executed; the full four-agent chain needs a key."
