#!/usr/bin/env bash
# Deploy skill trees to every fleet runner host.
#
# Trees are deploy-placed (TOML catalog design): ensure only checks that
# install_as exists and is non-empty. This script is the distribution channel.
#
# Usage:
#   deploy/fleet-skills.sh [--from DIR] [--also-rclone] host [host ...]
#   PORTUNUS_FLEET_HOSTS="runner-a runner-b" deploy/fleet-skills.sh
#
# --from DIR  Source of skill trees (default: /var/lib/portunus/skills if it
#             contains at least one tree, else <repo>/third_party).
#             Each immediate subdirectory (except rclone-shim unless
#             --also-rclone) is rsynced to $host:/var/lib/portunus/skills/<name>/.
#
# Typical (ops host that already has production trees):
#   deploy/fleet-skills.sh runner-a runner-b
#
# After sync, verifies a known entrypoint when present and ensures skill-state
# dirs exist (empty shells only — never wipe tokens). Options must precede hosts.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SKILLS_DST="/var/lib/portunus/skills"
STATE_DST="/var/lib/portunus/skill-state"
SSH_OPTS=(-o ConnectTimeout=15 -o BatchMode=yes)
# Drop-privs runners write token/profile state as this uid (see ensure-state-dirs /
# provision-rclone-shim). Override with PORTUNUS_STATE_UID when needed.
STATE_UID="${PORTUNUS_STATE_UID:-10001}"
ALSO_RCLONE=0
FROM=""
if ! [[ "$STATE_UID" =~ ^[0-9]+$ ]]; then
  echo "PORTUNUS_STATE_UID must be numeric (got '$STATE_UID')" >&2
  exit 2
fi
if [[ "$STATE_UID" == "0" ]]; then
  echo "warning: PORTUNUS_STATE_UID=0 — skill-state will be root-owned (ok only when children do not drop privileges)" >&2
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      [[ $# -ge 2 ]] || { echo "usage: $0 [--from DIR] [--also-rclone] host ..." >&2; exit 2; }
      FROM="$2"; shift 2 ;;
    --also-rclone)
      ALSO_RCLONE=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
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
  echo "usage: $0 [--from DIR] [--also-rclone] host [host ...]" >&2
  echo "   or: PORTUNUS_FLEET_HOSTS=\"runner-a runner-b\" $0" >&2
  exit 2
}
for h in "${HOSTS[@]}"; do
  if [[ "$h" == -* ]]; then
    echo "unexpected option after hosts: $h (options must precede host names)" >&2
    exit 2
  fi
done

# Resolve skill-tree source. Worker images often bind third_party at
# /app/third_party while PORTUNUS_REPO_ROOT points at /opt/stacks/portunus.
resolve_skills_from() {
  local cand
  if [[ -n "$FROM" ]]; then
    [[ -d "$FROM" ]] || { echo "source not a directory: $FROM" >&2; return 1; }
    printf '%s' "$FROM"
    return 0
  fi
  if [[ -d "$SKILLS_DST/guangyapan-drive" || -d "$SKILLS_DST/quarkclouddrive" ]]; then
    printf '%s' "$SKILLS_DST"
    return 0
  fi
  for cand in \
    "${PORTUNUS_SKILLS_FROM:-}" \
    "$ROOT/third_party" \
    /app/third_party \
    /opt/stacks/portunus/third_party
  do
    [[ -n "$cand" && -d "$cand" ]] || continue
    # Prefer a tree that actually contains skill dirs.
    if [[ -d "$cand/guangyapan-drive" || -d "$cand/quarkclouddrive" ]] \
      || find "$cand" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1 | grep -q .; then
      printf '%s' "$cand"
      return 0
    fi
  done
  echo "source not a directory: tried skills dst + third_party mounts" >&2
  return 1
}
FROM="$(resolve_skills_from)" || exit 2
echo "skills from: $FROM"

# Map tree directory name → skill-state slug (STATE_DIR leaf). Unknown trees
# still sync; state dir is created under the same basename as a fallback.
state_slug_for() {
  case "$1" in
    guangyapan-drive) echo guangyapan ;;
    quarkclouddrive) echo quark ;;
    proton-drive) echo proton-drive ;;
    agent-mail) echo agent-mail ;;
    rclone-shim) echo rclone ;;
    *) echo "$1" ;;
  esac
}

# Prefer a concrete entrypoint check when we know one.
entrypoint_for() {
  case "$1" in
    guangyapan-drive) echo bin/guangyapan.mjs ;;
    quarkclouddrive) echo scripts/quark-drive.cjs ;;
    proton-drive) echo scripts/proton-drive.cjs ;;
    agent-mail) echo scripts/agent-mail.cjs ;;
    rclone-shim) echo shim ;;
    *) echo "" ;;
  esac
}

TREES=()
for d in "$FROM"/*; do
  [[ -d "$d" ]] || continue
  name="$(basename "$d")"
  [[ "$name" == "rclone-shim" && "$ALSO_RCLONE" -eq 0 ]] && continue
  # Skip non-skill dirs under third_party.
  [[ "$name" == .* ]] && continue
  TREES+=("$name")
done
[[ ${#TREES[@]} -gt 0 ]] || { echo "no skill trees under $FROM" >&2; exit 2; }

echo "source: $FROM"
echo "trees:  ${TREES[*]}"
echo "hosts:  ${HOSTS[*]}"
echo "state uid: $STATE_UID"

fail=0
for host in "${HOSTS[@]}"; do
  echo "=== $host ==="
  if ! ssh "${SSH_OPTS[@]}" "$host" "mkdir -p '$SKILLS_DST' '$STATE_DST'"; then
    echo "FAIL: cannot reach $host" >&2
    fail=1
    continue
  fi
  for name in "${TREES[@]}"; do
    src="$FROM/$name"
    [[ -d "$src" ]] || { echo "skip missing $src"; continue; }
    echo "  rsync $name/"
    # Never --delete the provisioner-managed rclone binary (not in git / --from trees).
    rsync_opts=(-az -e "ssh ${SSH_OPTS[*]}")
    if [[ "$name" == "rclone-shim" ]]; then
      # Keep host-provisioned rclone binary; force shim executable for any child uid
      # (provision-rclone uses chmod 755 — owner-only +x is not enough under drop-privs).
      rsync_opts+=(--exclude rclone --chmod=F755,D755)
    else
      rsync_opts+=(--delete)
    fi
    if ! rsync "${rsync_opts[@]}" "$src/" "$host:$SKILLS_DST/$name/"; then
      echo "FAIL: rsync $name -> $host" >&2
      fail=1
      continue
    fi
    slug="$(state_slug_for "$name")"
    ep="$(entrypoint_for "$name")"
    # MCP children (drop-privs uid) must own skill-state — root:root 0700 breaks login capture.
    remote_cmd="mkdir -p '$STATE_DST/$slug' && chmod 700 '$STATE_DST/$slug' && chown '$STATE_UID:$STATE_UID' '$STATE_DST/$slug'"
    if [[ -n "$ep" ]]; then
      remote_cmd+=" && test -e '$SKILLS_DST/$name/$ep' && echo OK:$name/$ep"
    else
      remote_cmd+=" && test -d '$SKILLS_DST/$name' && test -n \"\$(ls -A '$SKILLS_DST/$name' 2>/dev/null)\" && echo OK:$name"
    fi
    if ! ssh "${SSH_OPTS[@]}" "$host" "$remote_cmd"; then
      echo "FAIL: verify $name on $host" >&2
      fail=1
    fi
  done
done

if [[ "$fail" -ne 0 ]]; then
  echo "fleet-skills: one or more hosts failed" >&2
  exit 1
fi
echo "fleet-skills: all hosts ok"
