#!/usr/bin/env bash
# Pre-create per-app state directories for plain-stdio children (no skill-bridge
# setup runs for them, and their token caches must survive child restarts).
#
# Usage: [PORTUNUS_STATE_UID=<uid>] deploy/ensure-state-dirs.sh <slug> [<slug> ...]
#
# Scope: stdio-connection slugs only (their env_defaults point token caches at
# skill-state/<slug>). Do NOT use for skill_bridge slugs: their login capture
# has the runner writing inside the dir too, with its own ownership contract.
#
# The dirs hold vendor token caches (OAuth refresh tokens): owner-only access,
# owned by the uid the MCP children actually run as. Resolution order:
#   1. PORTUNUS_STATE_UID (explicit override; use under PORTUNUS_DROP_PRIVS=1,
#      where children setuid inside the container and host probing cannot see it)
#   2. the image's `portunus` user uid (docker run --rm --entrypoint id …)
#   3. the runner container's configured user (via compose, not a hardcoded name)
set -euo pipefail
[[ $# -ge 1 ]] || { echo "usage: [PORTUNUS_STATE_UID=<uid>] $0 <slug> [<slug> ...]" >&2; exit 2; }

resolve_uid() {
  if [[ -n "${PORTUNUS_STATE_UID:-}" ]]; then
    echo "$PORTUNUS_STATE_UID"; return
  fi
  # `id -u` prints a bare uid. Probe the *child* user first — under
  # PORTUNUS_DROP_PRIVS=1 children setuid to portunus-mcp (spawn.py) — then the
  # image's unprivileged `portunus` (the account Dockerfile actually creates).
  local img="${PORTUNUS_IMAGE:-portunus:local}"
  # Never pull: uid resolution must not become a network operation.
  docker image inspect "$img" >/dev/null 2>&1 || { echo "note: $img absent locally; skipping image probe" >&2; return; }
  docker run --rm --entrypoint id "$img" -u portunus-mcp 2>/dev/null \
    || docker run --rm --entrypoint id "$img" -u portunus 2>/dev/null \
    || true
}

CHILD_UID="$(resolve_uid)"
if [[ -z "$CHILD_UID" ]]; then
  local_stack="${PORTUNUS_RUNNER_STACK:-/opt/stacks/portunus/docker-compose.runner.yml}"
  cid="$(docker compose -f "$local_stack" ps -q portunus-runner 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    CHILD_UID="$(docker inspect "$cid" --format '{{.Config.User}}' 2>/dev/null | cut -d: -f1 || true)"
  fi
fi
[[ "$CHILD_UID" =~ ^[0-9]+$ ]] || {
  echo "cannot resolve a numeric child uid (got '${CHILD_UID:-empty}'); set PORTUNUS_STATE_UID" >&2
  exit 1
}

# Validate every slug before mutating anything. Mirrors the gateway's
# SLUG_PATTERN (underscores/hyphens allowed, ASCII alnum start, <=63 chars).
for slug in "$@"; do
  [[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$ ]] || { echo "invalid slug: $slug" >&2; exit 2; }
done

if [[ "$CHILD_UID" == "0" ]]; then
  echo "warning: resolved child uid is 0 (privileged runner). Token caches will be" >&2
  echo "root-owned; correct for this mode, but set PORTUNUS_STATE_UID if children" >&2
  echo "run unprivileged (PORTUNUS_DROP_PRIVS=1)." >&2
fi

for slug in "$@"; do
  dir="/var/lib/portunus/skill-state/$slug"
  mkdir -p "$dir"
  chown -R "$CHILD_UID:$CHILD_UID" "$dir"
  # Recursive: hosts upgraded from the earlier world-writable version may hold
  # token caches with 0644 modes; owner-only is the stated intent.
  find "$dir" -type d -exec chmod 700 {} +
  find "$dir" -type f -exec chmod 600 {} +
  echo "ready: $dir (uid $CHILD_UID, dirs 700, files 600)"
done
