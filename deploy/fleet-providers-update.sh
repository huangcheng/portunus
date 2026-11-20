#!/usr/bin/env bash
# Sync provider runtimes to every fleet runner: rclone, skill trees, vendor CLIs
# (cubox-cli, …), and portunus_skill_bridge inside runner containers.
#
# This is the single “sync all tools/skills/configs to runners” entrypoint used by
# Temporal FleetProvidersUpdateWorkflow and POST /api/fleet/providers-update.
#
# Usage:
#   deploy/fleet-providers-update.sh [--components LIST] [--from DIR] host [host ...]
#   PORTUNUS_FLEET_HOSTS="runner-a runner-b" deploy/fleet-providers-update.sh
#
# --components LIST  Comma-separated subset of:
#                      rclone, skills, clis, himalaya, skill_bridge
#                    (default: all). Order is always
#                      rclone → skills → clis → himalaya → skill_bridge.
# --from DIR         Passed through to fleet-skills.sh as skill-tree source.
#
# Env:
#   PORTUNUS_RCLONE_CACHE     Writable dir for rclone zip/binary (default under
#                             /var/lib/portunus when deploy/ is read-only)
#   PORTUNUS_RCLONE_VERSION   Pin (default in provision-rclone-shim.sh)
#   PORTUNUS_VENDOR_CLI_CACHE Writable dir for cubox-cli etc.
#   (no version override: each manifest's cli.provision.tag wins)
#   PORTUNUS_RUNNER_CONTAINER Override runner container name on each host
#   PORTUNUS_STATE_UID        Skill-state owner uid (fleet-skills.sh)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SSH_OPTS=(-o ConnectTimeout=15 -o BatchMode=yes)
COMPONENTS="rclone,skills,clis,himalaya,skill_bridge"
FROM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --components)
      [[ $# -ge 2 ]] || { echo "usage: $0 [--components LIST] [--from DIR] host ..." >&2; exit 2; }
      COMPONENTS="$2"; shift 2 ;;
    --from)
      [[ $# -ge 2 ]] || { echo "usage: $0 [--components LIST] [--from DIR] host ..." >&2; exit 2; }
      FROM="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^[^#]/p' "$0" | sed '$d'; exit 0 ;;
    --)
      shift; break ;;
    -*)
      echo "unknown option: $1" >&2; exit 2 ;;
    *)
      break ;;
  esac
done

HOSTS=("$@")
if [[ ${#HOSTS[@]} -eq 0 && -n "${PORTUNUS_FLEET_HOSTS:-}" ]]; then
  # shellcheck disable=SC2206
  HOSTS=($PORTUNUS_FLEET_HOSTS)
fi
[[ ${#HOSTS[@]} -gt 0 ]] || {
  echo "usage: $0 [--components LIST] [--from DIR] host [host ...]" >&2
  echo "   or: PORTUNUS_FLEET_HOSTS=\"runner-a runner-b\" $0" >&2
  exit 2
}
for h in "${HOSTS[@]}"; do
  if [[ "$h" == -* ]]; then
    echo "unexpected option after hosts: $h (options must precede host names)" >&2
    exit 2
  fi
done

want_rclone=0
want_skills=0
want_clis=0
want_himalaya=0
want_bridge=0
IFS=',' read -r -a _parts <<<"$COMPONENTS"
for raw in "${_parts[@]}"; do
  c="$(echo "$raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "$c" in
    "" ) ;;
    rclone) want_rclone=1 ;;
    skills|skill|trees) want_skills=1 ;;
    clis|cli|tools|vendor|vendor_clis|vendor-clis) want_clis=1 ;;
    himalaya|himalaya-shim|imap) want_himalaya=1 ;;
    skill_bridge|skill-bridge|bridge) want_bridge=1 ;;
    *)
      echo "unknown component: $raw (want rclone,skills,clis,himalaya,skill_bridge)" >&2
      exit 2 ;;
  esac
done
[[ $want_rclone -eq 1 || $want_skills -eq 1 || $want_clis -eq 1 || $want_himalaya -eq 1 || $want_bridge -eq 1 ]] || {
  echo "no components selected" >&2
  exit 2
}

# Prefer a writable cache when deploy/ is mounted read-only (Temporal worker).
if [[ -z "${PORTUNUS_RCLONE_CACHE:-}" ]]; then
  if [[ -w "$HERE/rclone-shim" ]]; then
    export PORTUNUS_RCLONE_CACHE="$HERE/rclone-shim"
  else
    export PORTUNUS_RCLONE_CACHE="/var/lib/portunus/cache/rclone"
    mkdir -p "$PORTUNUS_RCLONE_CACHE"
  fi
fi

echo "components: rclone=$want_rclone skills=$want_skills clis=$want_clis himalaya=$want_himalaya skill_bridge=$want_bridge"
echo "hosts: ${HOSTS[*]}"
echo "rclone cache: $PORTUNUS_RCLONE_CACHE"

fail=0
# Per-component status for Temporal / operators: ok | fail | skip | na
st_rclone=na
st_skills=na
st_clis=na
st_himalaya=na
st_bridge=na

run_component() {
  # $1=name  $2=status_var  remaining=command
  local name="$1" var="$2"
  shift 2
  local rc=0
  set +e
  "$@"
  rc=$?
  set -e
  case "$rc" in
    0) printf -v "$var" '%s' "ok" ;;
    3)
      printf -v "$var" '%s' "skip"
      echo "SKIP: $name (non-fatal)" >&2
      ;;
    *)
      printf -v "$var" '%s' "fail"
      echo "FAIL: $name (exit $rc)" >&2
      fail=1
      ;;
  esac
  return 0
}

if [[ "$want_rclone" -eq 1 ]]; then
  echo "=== provision rclone ==="
  run_component rclone st_rclone bash "$HERE/provision-rclone-shim.sh" "${HOSTS[@]}"
fi

if [[ "$want_skills" -eq 1 ]]; then
  echo "=== sync skill trees ==="
  skills_args=(--also-rclone)
  if [[ -n "$FROM" ]]; then
    skills_args+=(--from "$FROM")
  fi
  run_component skills st_skills bash "$HERE/fleet-skills.sh" "${skills_args[@]}" "${HOSTS[@]}"
fi

if [[ "$want_clis" -eq 1 ]]; then
  echo "=== provision vendor CLIs (cubox-cli, …) ==="
  run_component clis st_clis bash "$HERE/provision-vendor-clis.sh" "${HOSTS[@]}"
fi

if [[ "$want_himalaya" -eq 1 ]]; then
  echo "=== provision himalaya-shim (IMAP/SMTP) ==="
  if [[ -z "${PORTUNUS_HIMALAYA_CACHE:-}" ]]; then
    if [[ -w "$HERE/himalaya-shim" ]]; then
      export PORTUNUS_HIMALAYA_CACHE="$HERE/himalaya-shim"
    else
      export PORTUNUS_HIMALAYA_CACHE="/var/lib/portunus/cache/himalaya"
      mkdir -p "$PORTUNUS_HIMALAYA_CACHE"
    fi
  fi
  # Local vultr runner is updated separately on-host (worker has no docker.sock /
  # self-SSH). Opt in only when this script runs on the host:
  #   PORTUNUS_LOCAL_RUNNER=1 deploy/fleet-providers-update.sh …
  if [[ -z "${PORTUNUS_APPS_DIR:-}" ]]; then
    for cand in "$ROOT/catalog/apps" /opt/stacks/portunus/catalog/apps; do
      if [[ -d "$cand" ]]; then
        export PORTUNUS_APPS_DIR="$cand"
        break
      fi
    done
  fi
  himalaya_hosts=("${HOSTS[@]}")
  include_local="${PORTUNUS_LOCAL_RUNNER:-}"
  case "${include_local,,}" in
    1|true|yes|on) include_local=1 ;;
    ""|0|false|no|off) include_local="" ;;
    *)
      echo "ignoring unrecognized PORTUNUS_LOCAL_RUNNER=$include_local" >&2
      include_local=""
      ;;
  esac
  if [[ "$include_local" == "1" ]] && \
     [[ " ${HOSTS[*]} " != *" local "* && " ${HOSTS[*]} " != *" localhost "* && " ${HOSTS[*]} " != *" 127.0.0.1 "* ]]; then
    himalaya_hosts+=(local)
  fi
  run_component himalaya st_himalaya bash "$HERE/provision-himalaya-shim.sh" "${himalaya_hosts[@]}"
fi

resolve_skill_bridge_src() {
  if [[ -n "${PORTUNUS_SKILL_BRIDGE_SRC:-}" && -d "${PORTUNUS_SKILL_BRIDGE_SRC}" ]]; then
    echo "$PORTUNUS_SKILL_BRIDGE_SRC"
    return 0
  fi
  local cand
  for cand in "$ROOT/packages/portunus_skill_bridge" /app/packages/portunus_skill_bridge; do
    if [[ -d "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

sync_skill_bridge_host() {
  local host="$1"
  local src
  src="$(resolve_skill_bridge_src)" || { echo "missing portunus_skill_bridge package" >&2; return 1; }
  local remote_tmp="/tmp/portunus-patch/skill_bridge"
  echo "  skill_bridge → $host"
  ssh "${SSH_OPTS[@]}" "$host" "mkdir -p '$remote_tmp'" || return 1
  rsync -az --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' --exclude 'tests' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$src/" "$host:$remote_tmp/" || return 1
  # Prefer explicit container name; else first running *portunus*runner* name.
  # Interpolate override — ssh does not forward local PORTUNUS_RUNNER_CONTAINER.
  local runner_override
  runner_override="$(printf '%q' "${PORTUNUS_RUNNER_CONTAINER:-}")"
  ssh "${SSH_OPTS[@]}" "$host" "set -e
    C=$runner_override
    if [[ -z \"\$C\" ]]; then
      C=\$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'portunus.*runner' | head -1 || true)
    fi
    if [[ -z \"\$C\" ]]; then
      echo 'FAIL: no runner container (set PORTUNUS_RUNNER_CONTAINER)' >&2
      exit 1
    fi
    docker cp '$remote_tmp'/. \"\$C:/app/packages/portunus_skill_bridge/\"
    docker restart \"\$C\" >/dev/null
    echo OK:skill_bridge:\$C
  "
}

if [[ "$want_bridge" -eq 1 ]]; then
  echo "=== sync portunus_skill_bridge ==="
  st_bridge=ok
  for host in "${HOSTS[@]}"; do
    if ! sync_skill_bridge_host "$host"; then
      echo "FAIL: skill_bridge on $host" >&2
      st_bridge=fail
      fail=1
    fi
  done
fi

# Machine-readable summary for Temporal activity / operators.
echo "COMPONENT_STATUS rclone=$st_rclone skills=$st_skills clis=$st_clis himalaya=$st_himalaya skill_bridge=$st_bridge"

if [[ "$fail" -ne 0 ]]; then
  echo "fleet-providers-update: one or more steps failed" >&2
  exit 1
fi
echo "fleet-providers-update: all requested components ok (skips allowed)"
