#!/usr/bin/env bash
# Portunus end-to-end check: stdio fixture server -> probe -> enable -> grant -> invoke via /mcp.
#
# Managed mode (default): starts a runner + gateway in PORTUNUS_DEV=1 mode inside a temp dir,
# runs the scenario, and tears everything down. Needs `uv` and `python3`; nothing else.
#
# External mode: set PORTUNUS_URL (and PORTUNUS_OPERATOR_PASSWORD) to run against a stack you
# started yourself (e.g. docker compose). Then E2E_COMMAND must be an absolute path that is in
# the runner's binary_allowlist and speaks MCP over stdio (no args allowed), and
# E2E_EXPECT_TOOL_TEXT may be overridden if it is not the bundled fixture.
#
# Exits non-zero on the first unexpected result.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLUG="e2e-fixture"
FAILED=0
WORK=""
PIDS=()

log() { printf '\033[1;34m[e2e]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[e2e] FAIL:\033[0m %s\n' "$*" >&2; FAILED=1; exit 1; }

cleanup() {
  local rc=$?
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
  done
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    if [[ $rc -ne 0 ]]; then
      log "logs kept in $WORK (gateway.log, runner.log)"
    else
      rm -rf "$WORK"
    fi
  fi
  exit $rc
}
trap cleanup EXIT

command -v python3 >/dev/null || fail "python3 is required"
command -v curl >/dev/null || fail "curl is required"

# ---- stack ---------------------------------------------------------------------------

if [[ -z "${PORTUNUS_URL:-}" ]]; then
  command -v uv >/dev/null || fail "uv is required for managed mode"
  WORK="$(python3 -c 'import os,tempfile;print(os.path.realpath(tempfile.mkdtemp(prefix="pe2e-",dir="/tmp")))')"
  PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
  PORTUNUS_URL="http://127.0.0.1:${PORT}"
  PORTUNUS_OPERATOR_PASSWORD="e2e-$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')"

  PY="$(cd "$ROOT/gateway" && uv run --frozen python -c 'import sys;print(sys.executable)')"
  E2E_COMMAND="$WORK/fixture-mcp"
  printf '#!/bin/sh\nexec "%s" "%s"\n' "$PY" "$ROOT/gateway/tests/fixtures/fake_mcp_server.py" >"$E2E_COMMAND"
  chmod 0755 "$E2E_COMMAND"
  printf '{"trusted_path": ["/usr/bin", "/bin"], "binary_allowlist": ["%s"]}\n' "$E2E_COMMAND" >"$WORK/runner-policy.json"

  python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())' >"$WORK/pepper"
  chmod 0600 "$WORK/pepper"
  python3 -c 'import secrets;print(secrets.token_urlsafe(32))' >"$WORK/runner.token"
  chmod 0600 "$WORK/runner.token"
  SECRETS_KEY="k1:$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"

  export PORTUNUS_DEV=1
  export PORTUNUS_SOCKET="$WORK/runner.sock"
  export PORTUNUS_TOKEN_FILE="$WORK/runner.token"
  export PORTUNUS_POLICY="$WORK/runner-policy.json"

  log "starting runner (dev mode) in $WORK"
  (cd "$ROOT/gateway" && exec uv run --frozen portunus-runner) >"$WORK/runner.log" 2>&1 &
  PIDS+=($!)

  log "starting gateway on $PORTUNUS_URL"
  (
    cd "$ROOT/gateway"
    export PORTUNUS_SECRETS_KEY="$SECRETS_KEY"
    export PORTUNUS_PEPPER_FILE="$WORK/pepper"
    export PORTUNUS_OPERATOR_PASSWORD
    export PORTUNUS_DATABASE_URL="sqlite+aiosqlite:///$WORK/portunus.db"
    export PORTUNUS_BIND="127.0.0.1:${PORT}"
    exec uv run --frozen python -m portunus_gateway
  ) >"$WORK/gateway.log" 2>&1 &
  PIDS+=($!)
else
  : "${PORTUNUS_OPERATOR_PASSWORD:?set PORTUNUS_OPERATOR_PASSWORD for external mode}"
  : "${E2E_COMMAND:?set E2E_COMMAND (absolute, allowlisted stdio MCP binary) for external mode}"
  WORK="$(mktemp -d /tmp/pe2e-XXXXXX)"
fi

BASE="${PORTUNUS_URL%/}"
JAR="$WORK/cookies.txt"

log "waiting for $BASE/healthz"
ready=0
for _ in $(seq 1 120); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ready=1; break; fi
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null && { tail -n 30 "$WORK"/*.log >&2 || true; fail "a service exited during startup"; }
  done
  sleep 0.5
done
[[ $ready -eq 1 ]] || { tail -n 30 "$WORK"/*.log >&2 || true; fail "gateway did not become healthy"; }

# ---- helpers -------------------------------------------------------------------------

jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for p in sys.argv[1].split("."):
    d=d[int(p)] if isinstance(d,list) else d[p]
print(d if not isinstance(d,(dict,list)) else json.dumps(d))' "$1"; }

CSRF=""
STEP=""
# api METHOD PATH [JSON]  -> prints body; fails on non-2xx
api() {
  local method="$1" path="$2" body="${3:-}" out status
  local args=(-sS -o "$WORK/resp.json" -w '%{http_code}' -X "$method" -b "$JAR" -c "$JAR"
    -H "X-CSRF-Token: $CSRF" -H "X-Step-Up-Token: $STEP")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data "$body")
  status="$(curl "${args[@]}" "$BASE$path")" || fail "curl $method $path"
  if [[ "$status" != 2* ]]; then
    fail "$method $path -> HTTP $status: $(cat "$WORK/resp.json")"
  fi
  cat "$WORK/resp.json"
}

# stepped: fresh single-use step-up token, then the same call as api.
stepped() {
  STEP="$(api POST /api/auth/step-up "$PW_JSON" | jget step_up_token)"
  [[ -n "$STEP" ]] || fail "no step-up token"
  api "$@"
  STEP=""
}

# mcp TOKEN TOOL ARGS_JSON -> prints the JSON-RPC response body (stateless Streamable HTTP)
mcp() {
  local token="$1" tool="$2" args="$3"
  curl -sS "$BASE/mcp" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    --data "$(python3 -c 'import json,sys
print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$tool" "$args")"
}

# tool_payload: JSON-RPC response on stdin -> the tool's JSON text payload (prints "ISERROR:" prefix on error)
tool_payload() { python3 -c 'import json,sys
r=json.load(sys.stdin)["result"]
t=r["content"][0]["text"]
print(("ISERROR:" if r.get("isError") else "")+t)'; }

# ---- scenario ------------------------------------------------------------------------

if [[ -f "$ROOT/dashboard/dist/index.html" && ${#PIDS[@]} -gt 0 ]]; then
  log "dashboard is served"
  curl -fsS "$BASE/" | grep -qi '<div id="root"' || fail "dashboard index.html not served at /"
fi

log "operator login"
PW_JSON="$(PW="$PORTUNUS_OPERATOR_PASSWORD" python3 -c 'import json,os;print(json.dumps({"password":os.environ["PW"]}))')"
LOGIN="$(curl -fsS -c "$JAR" -H 'Content-Type: application/json' --data "$PW_JSON" "$BASE/api/auth/login")" \
  || fail "operator login rejected"
CSRF="$(printf '%s' "$LOGIN" | jget csrf_token)"
[[ -n "$CSRF" ]] || fail "no csrf token"

log "mint agent keys"
KEY_JSON="$(stepped POST /api/keys '{"label":"e2e"}')"
TOKEN="$(printf '%s' "$KEY_JSON" | jget token)"
KEY_ID="$(printf '%s' "$KEY_JSON" | jget key_id)"
[[ "$TOKEN" == pm-* ]] || fail "unexpected agent token shape"
OTHER_TOKEN="$(stepped POST /api/keys '{"label":"e2e-ungranted"}' | jget token)"

log "register stdio fixture server ($E2E_COMMAND)"
BODY="$(SLUG="$SLUG" CMD="$E2E_COMMAND" python3 -c 'import json,os
print(json.dumps({"name":"E2E fixture","slug":os.environ["SLUG"],"kind":"stdio","command":os.environ["CMD"],"args":[]}))')"
SERVER_ID="$(api POST /api/servers "$BODY" | jget id)"

log "unenabled server is invisible to the agent"
OUT="$(mcp "$TOKEN" portunus_invoke_tool "{\"server\":\"$SLUG\",\"tool\":\"alpha\",\"arguments\":{}}" | tool_payload)"
[[ "$OUT" == ISERROR:* ]] || fail "invoke before grant/enable unexpectedly succeeded: $OUT"

log "probe"
api POST "/api/servers/$SERVER_ID/probe" >/dev/null
TOOLS="$(api GET "/api/servers/$SERVER_ID/tools")"
ALPHA_ID="$(printf '%s' "$TOOLS" | python3 -c 'import json,sys
t=[x for x in json.load(sys.stdin) if x["name"]=="alpha"]
assert t, "probe did not discover tool alpha"
assert t[0]["enabled"] is False, "probed tool must start disabled"
print(t[0]["id"])')" || fail "probe result unexpected: $TOOLS"

log "enable tool alpha + server, grant key"
stepped POST "/api/tools/$ALPHA_ID/enable" '{"enabled":true}' >/dev/null
stepped POST "/api/servers/$SERVER_ID/enable" '{"enabled":true}' >/dev/null
api PUT "/api/keys/$KEY_ID/grants/$SERVER_ID" >/dev/null

log "discover via /mcp"
OUT="$(mcp "$TOKEN" portunus_discover_servers '{}' | tool_payload)"
printf '%s' "$OUT" | python3 -c 'import json,sys
d=json.load(sys.stdin)
assert [i["slug"] for i in d["items"]]==[sys.argv[1]], d' "$SLUG" || fail "discover_servers unexpected: $OUT"
OUT="$(mcp "$TOKEN" portunus_discover_tools "{\"server\":\"$SLUG\"}" | tool_payload)"
printf '%s' "$OUT" | python3 -c 'import json,sys
d=json.load(sys.stdin)
assert [i["name"] for i in d["items"]]==["alpha"], d   # beta was never enabled' || fail "discover_tools unexpected: $OUT"

log "invoke alpha via /mcp"
OUT="$(mcp "$TOKEN" portunus_invoke_tool "{\"server\":\"$SLUG\",\"tool\":\"alpha\",\"arguments\":{\"q\":\"hello portunus\"}}" | tool_payload)"
# tool_payload yields the fixture's own text (alpha echoes its arguments as JSON); an error would
# carry the ISERROR: prefix.
printf '%s' "$OUT" | python3 -c 'import json,sys
got=json.loads(sys.stdin.read())
assert got=={"q":"hello portunus"}, got' || fail "invoke result unexpected: $OUT"

log "negative checks"
OUT="$(mcp "$TOKEN" portunus_invoke_tool "{\"server\":\"$SLUG\",\"tool\":\"beta\",\"arguments\":{}}" | tool_payload)"
[[ "$OUT" == ISERROR:*tool_disabled* ]] || fail "disabled tool invoke should be tool_disabled: $OUT"
OUT="$(mcp "$OTHER_TOKEN" portunus_invoke_tool "{\"server\":\"$SLUG\",\"tool\":\"alpha\",\"arguments\":{}}" | tool_payload)"
[[ "$OUT" == ISERROR:*not_granted* ]] || fail "ungranted key should get not_granted: $OUT"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/mcp" -H 'Authorization: Bearer pm-nope' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' --data '{}')"
[[ "$CODE" == "401" ]] || fail "bad agent key should be 401, got $CODE"

log "revoke key -> /mcp refuses"
api POST "/api/keys/$KEY_ID/revoke" >/dev/null
CODE="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/mcp" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' --data '{}')"
[[ "$CODE" == "401" ]] || fail "revoked key should be 401, got $CODE"

printf '\033[1;32m[e2e] PASS\033[0m stdio fixture served through Omni /mcp\n' >&2
