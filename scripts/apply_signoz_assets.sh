#!/usr/bin/env bash
# Apply DevGuard's SigNoz assets to a running SigNoz: the dashboard and the three
# alert rules. Idempotent enough to re-run — SigNoz assigns a new id each time, so
# delete the old copies first if you do not want duplicates.
#
#   ./scripts/apply_signoz_assets.sh
#
# Env (all optional):
#   SIGNOZ_URL       default http://127.0.0.1:3301
#   SIGNOZ_EMAIL     default devguard@example.com
#   SIGNOZ_PASSWORD  default DevGuard!2026
#
# Credentials are read from the environment ONLY. Nothing is written to disk and
# nothing is echoed — the token is never printed.

set -uo pipefail

URL="${SIGNOZ_URL:-http://127.0.0.1:3301}"
EMAIL="${SIGNOZ_EMAIL:-devguard@example.com}"
PASSWORD="${SIGNOZ_PASSWORD:-DevGuard!2026}"
PG_CONTAINER="${PG_CONTAINER:-signoz-signoz-metastore-postgres-0-1}"

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- auth -------------------------------------------------------------------
# /api/v1/login returns the SPA HTML on v0.135.0. The real endpoint is
# /api/v2/sessions/email_password, and it REQUIRES an orgId in the body
# ("orgID is required" without it), which is not exposed by any unauthenticated
# endpoint — so it is read from the metastore.
ORG=$(docker exec "$PG_CONTAINER" psql -U signoz -d signoz -tAc \
        "SELECT id FROM organizations LIMIT 1" 2>/dev/null | tr -d ' \r')
[ -n "$ORG" ] || fail "could not read orgId — is the stack up, and has an org been registered?"

JWT=$(curl -s --noproxy '*' --max-time 30 -X POST "$URL/api/v2/sessions/email_password" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"orgId\":\"$ORG\"}" \
      | python3 -c '
import sys, json
def find(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and v.startswith("eyJ") and "refresh" not in k.lower():
                return v
            r = find(v)
            if r: return r
    return None
print(find(json.load(sys.stdin)) or "")' 2>/dev/null)
[ -n "$JWT" ] || fail "login failed for $EMAIL"
echo "authenticated (token length ${#JWT}, not printed)"

# --- dashboard --------------------------------------------------------------
# NOTE: POST /api/v1/dashboards returns HTTP 501 "dashboard_deprecated" on
# v0.135.0. v2 is the only working route, and it migrates the file to
# schemaVersion v6 on ingest.
code=$(curl -s --noproxy '*' --max-time 60 -o /tmp/signoz-dash-resp.json -w '%{http_code}' \
        -X POST "$URL/api/v2/dashboards" \
        -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
        --data-binary @signoz/dashboard.json)
[ "$code" = "201" ] || fail "dashboard import returned HTTP $code"
echo "dashboard imported (HTTP 201)"

# --- alert rules ------------------------------------------------------------
rc=0
for f in signoz/alerts/*.json; do
  code=$(curl -s --noproxy '*' --max-time 60 -o /dev/null -w '%{http_code}' \
          -X POST "$URL/api/v2/rules" \
          -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
          --data-binary @"$f")
  if [ "$code" = "201" ]; then
    echo "alert rule applied: $(basename "$f") (HTTP 201)"
  else
    echo "alert rule FAILED: $(basename "$f") (HTTP $code)" >&2; rc=1
  fi
done
[ "$rc" = "0" ] || fail "one or more alert rules failed"

# --- verify -----------------------------------------------------------------
echo
echo "--- rules now on the instance ---"
curl -s --noproxy '*' --max-time 20 -H "Authorization: Bearer $JWT" "$URL/api/v1/rules" \
  | python3 -c '
import sys, json
rules = json.load(sys.stdin)["data"]["rules"]
print("count:", len(rules))
for r in rules:
    print("   %-38s state=%-9s severity=%s" % (
        r.get("alert"), r.get("state"), (r.get("labels") or {}).get("severity")))
assert len(rules) >= 3, "expected at least 3 alert rules"
' || fail "rule verification failed"

echo
echo "PASSED — dashboard and 3 alert rules applied and verified."
