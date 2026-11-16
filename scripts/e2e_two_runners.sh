#!/usr/bin/env bash
# Portunus fleet check (local only): two runners + one gateway, soft region affinity.
#
#   runner A: site-a / region-a      runner B: site-b / region-b
#   gateway : PORTUNUS_RUNNERS lists both unix sockets (+ token files)
#
# Scenario:
#   1. register a stdio fixture server with preferred_regions=["region-a"]
#   2. both runners up  -> probe + invoke are served by A (the preferred region)
#   3. SIGKILL runner A -> (a) the already-running child is pinned to dead site-a: its invoke
#                          must return promptly (real runner_unavailable error), never hang;
#                          (b) a fresh server preferring region-a falls back to B
#                          (preferred_miss) and its invoke completes.
#
# Everything lives in a mktemp dir under /tmp: unix sockets, throw-away secrets, sqlite db. No
# network beyond 127.0.0.1, no real hostnames, nothing is written into the repo.
# Needs `uv`, `python3` and `curl`. Exits non-zero on the first unexpected result.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLUG="fleet-fixture"
WORK=""
PIDS=()
PID_A=""
PID_B=""

log() { printf '\033[1;34m[e2e-fleet]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[e2e-fleet] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

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
      log "logs kept in $WORK (gateway.log, runner-a.log, runner-b.log)"
    else
      rm -rf "$WORK"
    fi
  fi
  exit $rc
}
trap cleanup EXIT

command -v python3 >/dev/null || fail "python3 is required"
command -v curl >/dev/null || fail "curl is required"
command -v uv >/dev/null || fail "uv is required"

# ---- temp stack ----------------------------------------------------------------------

WORK="$(python3 -c 'import os,tempfile;print(os.path.realpath(tempfile.mkdtemp(prefix="pfleet-",dir="/tmp")))')"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://127.0.0.1:${PORT}"
PORTUNUS_OPERATOR_PASSWORD="e2e-$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')"

PY="$(cd "$ROOT/gateway" && uv run --frozen python -c 'import sys;print(sys.executable)')"
RUNNER_BIN="$(dirname "$PY")/portunus-runner"
[[ -x "$RUNNER_BIN" ]] || fail "portunus-runner entrypoint not found next to $PY"

# The fixture wrapper records its parent pid (= the runner that spawned it) before exec'ing the
# stdio MCP fixture, so the script can tell which runner served each child.
FIXTURE="$WORK/fixture-mcp"
SPAWN_LOG="$WORK/spawns.log"
printf '#!/bin/sh\necho "$PPID" >> "%s"\nexec "%s" "%s"\n' \
  "$SPAWN_LOG" "$PY" "$ROOT/gateway/tests/fixtures/fake_mcp_server.py" >"$FIXTURE"
chmod 0755 "$FIXTURE"
: >"$SPAWN_LOG"
printf '{"trusted_path": ["/usr/bin", "/bin"], "binary_allowlist": ["%s"]}\n' "$FIXTURE" >"$WORK/runner-policy.json"

python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())' >"$WORK/pepper"
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' >"$WORK/a.token"
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' >"$WORK/b.token"
chmod 0600 "$WORK/pepper" "$WORK/a.token" "$WORK/b.token"
SECRETS_KEY="k1:$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"

# start_runner NAME SITE REGION -> pid in $STARTED_PID
start_runner() {
  local name="$1" site="$2" region="$3"
  (
    export PORTUNUS_DEV=1
    export PORTUNUS_SOCKET="$WORK/$name.sock"
    export PORTUNUS_TOKEN_FILE="$WORK/$name.token"
    export PORTUNUS_POLICY="$WORK/runner-policy.json"
    export PORTUNUS_SITE_ID="$site"
    export PORTUNUS_REGIONS="$region"
    cd "$ROOT/gateway"
    exec "$RUNNER_BIN"
  ) >"$WORK/runner-$name.log" 2>&1 &
  STARTED_PID=$!
  PIDS+=("$STARTED_PID")
}

log "starting runners site-a/region-a and site-b/region-b in $WORK"
start_runner a site-a region-a
PID_A="$STARTED_PID"
start_runner b site-b region-b
PID_B="$STARTED_PID"

log "starting gateway on $BASE with both runners in the pool"
(
  cd "$ROOT/gateway"
  export PORTUNUS_DEV=1
  export PORTUNUS_RUNNERS="$WORK/a.sock:$WORK/a.token,$WORK/b.sock:$WORK/b.token"
  export PORTUNUS_POLICY="$WORK/runner-policy.json"
  export PORTUNUS_SECRETS_KEY="$SECRETS_KEY"
  export PORTUNUS_PEPPER_FILE="$WORK/pepper"
  export PORTUNUS_OPERATOR_PASSWORD
  export PORTUNUS_DATABASE_URL="sqlite+aiosqlite:///$WORK/portunus.db"
  export PORTUNUS_BIND="127.0.0.1:${PORT}"
  exec "$PY" -m portunus_gateway
) >"$WORK/gateway.log" 2>&1 &
GATEWAY_PID=$!
PIDS+=("$GATEWAY_PID")

# ---- helpers -------------------------------------------------------------------------

JAR="$WORK/cookies.txt"
CSRF=""
STEP=""

jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for p in sys.argv[1].split("."):
    d=d[int(p)] if isinstance(d,list) else d[p]
print(d if not isinstance(d,(dict,list)) else json.dumps(d))' "$1"; }

api() {
  local method="$1" path="$2" body="${3:-}" status
  local args=(-sS -o "$WORK/resp.json" -w '%{http_code}' --max-time 60 -X "$method" -b "$JAR" -c "$JAR"
    -H "X-CSRF-Token: $CSRF" -H "X-Step-Up-Token: $STEP")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data "$body")
  status="$(curl "${args[@]}" "$BASE$path")" || fail "curl $method $path"
  [[ "$status" == 2* ]] || fail "$method $path -> HTTP $status: $(cat "$WORK/resp.json")"
  cat "$WORK/resp.json"
}

# stepped: fresh single-use step-up token, then the same call as api.
stepped() {
  STEP="$(api POST /api/auth/step-up "$PW_JSON" | jget step_up_token)"
  [[ -n "$STEP" ]] || fail "no step-up token"
  api "$@"
  STEP=""
}

# mcp TOKEN TOOL ARGS_JSON -> JSON-RPC response body. Bounded by --max-time so a hang is a failure.
MCP_MAX_TIME="${E2E_MCP_MAX_TIME:-45}"
mcp() {
  local token="$1" tool="$2" args="$3"
  curl -sS --max-time "$MCP_MAX_TIME" "$BASE/mcp" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    --data "$(python3 -c 'import json,sys
print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$tool" "$args")"
}

tool_payload() { python3 -c 'import json,sys
r=json.load(sys.stdin)["result"]
t=r["content"][0]["text"]
print(("ISERROR:" if r.get("isError") else "")+t)'; }

healthz_runners() { curl -fsS --max-time 3 "$BASE/healthz" 2>/dev/null | python3 -c 'import json,sys
print(json.load(sys.stdin).get("runners_connected", -1))' 2>/dev/null || echo -1; }

# wait_runners N: poll until the gateway sees exactly N live runners
wait_runners() {
  local want="$1" tries="${2:-120}"
  for _ in $(seq 1 "$tries"); do
    [[ "$(healthz_runners)" == "$want" ]] && return 0
    for pid in "${PIDS[@]:-}"; do
      # a service dying is only fatal for the ones we did not kill on purpose
      [[ -n "$pid" && "$pid" != "$PID_A_KILLED" ]] && ! kill -0 "$pid" 2>/dev/null \
        && { tail -n 30 "$WORK"/*.log >&2 || true; fail "a service exited unexpectedly"; }
    done
    sleep 0.25
  done
  tail -n 30 "$WORK"/*.log >&2 || true
  fail "gateway never reported $want connected runner(s) (last: $(healthz_runners))"
}
PID_A_KILLED=""

# spawn_owners -> one line per recorded child spawn: "A", "B" or "?"
spawn_owners() {
  while read -r ppid; do
    [[ -z "$ppid" ]] && continue
    if [[ "$ppid" == "$PID_A" ]]; then echo A; elif [[ "$ppid" == "$PID_B" ]]; then echo B; else echo '?'; fi
  done <"$SPAWN_LOG"
}

# ---- scenario ------------------------------------------------------------------------

log "waiting for gateway + both runners"
wait_runners 2

log "operator login / step-up / mint key"
PW_JSON="$(PW="$PORTUNUS_OPERATOR_PASSWORD" python3 -c 'import json,os;print(json.dumps({"password":os.environ["PW"]}))')"
LOGIN="$(curl -fsS --max-time 10 -c "$JAR" -H 'Content-Type: application/json' --data "$PW_JSON" "$BASE/api/auth/login")" \
  || fail "operator login rejected"
CSRF="$(printf '%s' "$LOGIN" | jget csrf_token)"
KEY_JSON="$(stepped POST /api/keys '{"label":"e2e-fleet"}')"
TOKEN="$(printf '%s' "$KEY_JSON" | jget token)"
KEY_ID="$(printf '%s' "$KEY_JSON" | jget key_id)"

log "register fixture server with preferred_regions=[region-a]"
BODY="$(SLUG="$SLUG" CMD="$FIXTURE" python3 -c 'import json,os
print(json.dumps({"name":"Fleet fixture","slug":os.environ["SLUG"],"kind":"stdio","command":os.environ["CMD"],"args":[],"preferred_regions":["region-a"]}))')"
SERVER_JSON="$(api POST /api/servers "$BODY")"
SERVER_ID="$(printf '%s' "$SERVER_JSON" | jget id)"
printf '%s' "$SERVER_JSON" | python3 -c 'import json,sys
assert json.load(sys.stdin)["preferred_regions"]==["region-a"]' || fail "preferred_regions not stored"

log "probe, enable, grant"
api POST "/api/servers/$SERVER_ID/probe" >/dev/null
ALPHA_ID="$(api GET "/api/servers/$SERVER_ID/tools" | python3 -c 'import json,sys
t=[x for x in json.load(sys.stdin) if x["name"]=="alpha"]
assert t, "probe did not discover alpha"
print(t[0]["id"])')" || fail "probe result unexpected"
stepped POST "/api/tools/$ALPHA_ID/enable" '{"enabled":true}' >/dev/null
stepped POST "/api/servers/$SERVER_ID/enable" '{"enabled":true}' >/dev/null
api PUT "/api/keys/$KEY_ID/grants/$SERVER_ID" >/dev/null

invoke_alpha() {
  mcp "$TOKEN" portunus_invoke_tool \
    "{\"server\":\"$SLUG\",\"tool\":\"alpha\",\"arguments\":{\"q\":\"$1\"}}" | tool_payload
}

log "both runners up: invoke must be scheduled on A (region-a)"
OUT="$(invoke_alpha "with-both-up")" || fail "invoke request failed"
printf '%s' "$OUT" | python3 -c 'import json,sys
assert json.loads(sys.stdin.read())=={"q":"with-both-up"}' || fail "invoke result unexpected: $OUT"
OWNERS="$(spawn_owners | sort -u | tr '\n' ' ')"
[[ "$OWNERS" == "A " ]] || fail "expected every spawn (probe + invoke) on runner A, got: ${OWNERS:-none}"
log "ok: spawns so far all on site-a"
SPAWNS_BEFORE="$(wc -l <"$SPAWN_LOG" | tr -d ' ')"

log "killing runner A (SIGKILL) -- preferred region goes away"
PID_A_KILLED="$PID_A"
kill -9 "$PID_A"
wait "$PID_A" 2>/dev/null || true
wait_runners 1 60

log "invoke on the already-running server after A is gone: must return promptly (real error or served), never hang"
START="$SECONDS"
RC=0
OUT="$(invoke_alpha "after-kill-a")" || RC=$?
ELAPSED=$((SECONDS - START))
[[ $RC -eq 0 ]] || fail "invoke hung or transport failed after killing A (curl rc=$RC after ${ELAPSED}s)"
if [[ "$OUT" == ISERROR:* ]]; then
  # Children are pinned to the site that started them, so a live child on the dead site yields an
  # honest runner_unavailable rather than a silent re-route. It must be a real, non-empty error.
  [[ "$OUT" == *runner_unavailable* ]] || fail "unexpected error shape after killing A: $OUT"
  log "ok: pinned child -> real error in ${ELAPSED}s: ${OUT:0:160}"
else
  printf '%s' "$OUT" | python3 -c 'import json,sys
assert json.loads(sys.stdin.read())=={"q":"after-kill-a"}' || fail "invoke result unexpected: $OUT"
  log "ok: served in ${ELAPSED}s"
fi

# A fresh server with the same preference has no pinned child: the scheduler must fall back to
# B (preferred_miss) and the invoke must complete.
SLUG2="fleet-fixture-2"
log "register second server (preferred_regions=[region-a]) while A is down -> must fall back to B"
BODY2="$(SLUG="$SLUG2" CMD="$FIXTURE" python3 -c 'import json,os
print(json.dumps({"name":"Fleet fixture 2","slug":os.environ["SLUG"],"kind":"stdio","command":os.environ["CMD"],"args":[],"preferred_regions":["region-a"]}))')"
SERVER2_ID="$(api POST /api/servers "$BODY2" | jget id)"
api POST "/api/servers/$SERVER2_ID/probe" >/dev/null || fail "probe on fallback runner failed"
ALPHA2_ID="$(api GET "/api/servers/$SERVER2_ID/tools" | python3 -c 'import json,sys
print([x for x in json.load(sys.stdin) if x["name"]=="alpha"][0]["id"])')"
stepped POST "/api/tools/$ALPHA2_ID/enable" '{"enabled":true}' >/dev/null
stepped POST "/api/servers/$SERVER2_ID/enable" '{"enabled":true}' >/dev/null
api PUT "/api/keys/$KEY_ID/grants/$SERVER2_ID" >/dev/null

START="$SECONDS"
RC=0
OUT="$(mcp "$TOKEN" portunus_invoke_tool \
  "{\"server\":\"$SLUG2\",\"tool\":\"alpha\",\"arguments\":{\"q\":\"fallback\"}}" | tool_payload)" || RC=$?
ELAPSED=$((SECONDS - START))
[[ $RC -eq 0 ]] || fail "fallback invoke hung or transport failed (curl rc=$RC after ${ELAPSED}s)"
printf '%s' "$OUT" | python3 -c 'import json,sys
assert json.loads(sys.stdin.read())=={"q":"fallback"}' || fail "fallback invoke result unexpected: $OUT"
NEW_OWNERS="$(tail -n +"$((SPAWNS_BEFORE + 1))" "$SPAWN_LOG" | while read -r p; do
  if [[ "$p" == "$PID_B" ]]; then echo B; elif [[ "$p" == "$PID_A" ]]; then echo A; else echo '?'; fi
done | sort -u | tr '\n' ' ')"
[[ "$NEW_OWNERS" == "B " ]] || fail "fallback children were not all spawned by runner B (owners: ${NEW_OWNERS:-none})"
log "ok: preferred_miss fallback served by site-b in ${ELAPSED}s"

printf '\033[1;32m[e2e-fleet] PASS\033[0m soft affinity: A preferred when up; after kill-A a fresh server falls back to B\n' >&2
