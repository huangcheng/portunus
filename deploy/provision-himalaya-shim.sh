#!/usr/bin/env bash
# Provision the shared Himalaya IMAP/SMTP shim on runner hosts:
#   deploy/provision-himalaya-shim.sh [user@]host [...]
#
# Installs under /var/lib/portunus/skills/himalaya-shim/:
#   run, himalaya_config.py  (from deploy/himalaya-shim/)
#   himalaya                 (GitHub release binary)
#
# Exit 3 = skip (download failed) so fleet sync can continue other components.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HIMALAYA_VERSION="${PORTUNUS_HIMALAYA_VERSION:-v2.2.1}"
SHIM_DIR="/var/lib/portunus/skills/himalaya-shim"
STATE_ROOT="/var/lib/portunus/skill-state"
STATE_UID="${PORTUNUS_STATE_UID:-10001}"
SSH_OPTS=(-o ConnectTimeout=10)
CACHE_DIR="${PORTUNUS_HIMALAYA_CACHE:-$HERE/himalaya-shim}"
mkdir -p "$CACHE_DIR"
[[ $# -ge 1 ]] || { echo "usage: $0 [user@]host [...]" >&2; exit 2; }
[[ -f "$HERE/himalaya-shim/run" ]] || { echo "missing $HERE/himalaya-shim/run" >&2; exit 2; }
[[ -f "$HERE/himalaya-shim/himalaya_config.py" ]] || {
  echo "missing $HERE/himalaya-shim/himalaya_config.py" >&2
  exit 2
}
if ! [[ "$STATE_UID" =~ ^[0-9]+$ ]]; then
  echo "PORTUNUS_STATE_UID must be numeric (got '$STATE_UID')" >&2
  exit 2
fi
if [[ "$STATE_UID" == "0" ]]; then
  echo "warning: PORTUNUS_STATE_UID=0 — skill-state will be root-owned" >&2
fi

# Discover provider STATE_DIR leaves from catalog TOMLs so a new mail recipe
# (and a new fleet host) get mkdir without editing this script.
# Override: PORTUNUS_HIMALAYA_STATE_SLUGS="slug-a slug-b"
discover_state_slugs() {
  local -a found=()
  if [[ -n "${PORTUNUS_HIMALAYA_STATE_SLUGS:-}" ]]; then
    local tok valid=0
    # shellcheck disable=SC2086
    for tok in $PORTUNUS_HIMALAYA_STATE_SLUGS; do
      if ! [[ "$tok" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$ ]]; then
        echo "ignoring invalid PORTUNUS_HIMALAYA_STATE_SLUGS token: $tok" >&2
        continue
      fi
      found+=("$tok")
      valid=1
    done
    if [[ "$valid" -ne 1 ]]; then
      echo "warn: PORTUNUS_HIMALAYA_STATE_SLUGS had no valid tokens; falling back" >&2
    fi
  fi
  if [[ ${#found[@]} -eq 0 ]]; then
    local apps_dir="" cand slug
    for cand in \
      "${PORTUNUS_APPS_DIR:-}" \
      "$HERE/../catalog/apps" \
      /opt/stacks/portunus/catalog/apps \
      /app/catalog/apps
    do
      [[ -n "$cand" && -d "$cand" ]] || continue
      apps_dir="$cand"
      break
    done
    if [[ -z "$apps_dir" ]]; then
      echo "warn: no catalog/apps dir found (set PORTUNUS_APPS_DIR); using builtin mail slugs" >&2
    else
      local f
      for f in "$apps_dir"/*.toml; do
        [[ -f "$f" ]] || continue
        # Himalaya IMAP providers set IMAP_URL (or reference himalaya in tags/manifest).
        grep -qE '^[[:space:]]*IMAP_URL[[:space:]]*=' "$f" || grep -qiE 'himalaya' "$f" || continue
        local line slug
        line="$(grep -E '^[[:space:]]*STATE_DIR[[:space:]]*=' "$f" | head -1 || true)"
        [[ -n "$line" ]] || continue
        # Quote-agnostic: strip trailing ' or " from the leaf.
        slug="$(sed -E 's|.*/skill-state/([^\"'\''/[:space:]]+).*|\1|' <<<"$line")"
        if ! [[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$ ]]; then
          echo "warn: cannot parse STATE_DIR in $f: $line" >&2
          continue
        fi
        found+=("$slug")
      done
    fi
  fi
  if [[ ${#found[@]} -eq 0 ]]; then
    found=(qqmail-imap vivaldi-mail)
  fi
  # Dedup while preserving order
  printf '%s\n' "${found[@]}" | awk 'NF && !seen[$0]++'
}

mapfile -t HIMALAYA_STATE_SLUG_LIST < <(discover_state_slugs)
echo "himalaya STATE_DIR leaves: ${HIMALAYA_STATE_SLUG_LIST[*]}"

fetch_himalaya() {
  local arch="$1"  # x86_64 | aarch64
  local asset="himalaya.${arch}-linux.tgz"
  local url="https://github.com/pimalaya/himalaya/releases/download/${HIMALAYA_VERSION}/${asset}"
  local dest="$CACHE_DIR/himalaya.${arch}"
  if [[ -x "$dest" ]]; then
    return 0
  fi
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/portunus-himalaya.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  echo "  fetch $url"
  if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "$tmp/asset.tgz" "$url"; then
    echo "download failed: $url" >&2
    return 1
  fi
  tar -xzf "$tmp/asset.tgz" -C "$tmp" || return 1
  local found
  found="$(find "$tmp" -type f -name himalaya | head -1)"
  [[ -n "$found" ]] || { echo "archive missing himalaya binary" >&2; return 1; }
  install -m 755 "$found" "$dest"
  printf '%s' "$HIMALAYA_VERSION" >"$CACHE_DIR/himalaya.${arch}.version"
}

is_local_host() {
  case "$1" in
    local|localhost|127.0.0.1) return 0 ;;
    *) return 1 ;;
  esac
}

fail=0
for host in "$@"; do
  echo "=== himalaya-shim $host ==="
  if [[ -z "$host" ]]; then
    echo "FAIL: empty host entry" >&2
    fail=1
    continue
  fi
  if is_local_host "$host"; then
    arch_raw="$(uname -m)"
  else
    arch_raw="$(ssh "${SSH_OPTS[@]}" "$host" 'uname -m')" || {
      echo "FAIL: cannot reach $host" >&2
      fail=1
      continue
    }
  fi
  case "$arch_raw" in
    x86_64|amd64) GARCH=x86_64 ;;
    aarch64|arm64) GARCH=aarch64 ;;
    *)
      echo "FAIL: unsupported arch on $host: $arch_raw" >&2
      fail=1
      continue
      ;;
  esac
  if ! fetch_himalaya "$GARCH"; then
    echo "SKIP: himalaya download failed for $GARCH" >&2
    exit 3
  fi
  ensure_state_dirs_cmd="mkdir -p '$STATE_ROOT'"
  for slug in "${HIMALAYA_STATE_SLUG_LIST[@]}"; do
    ensure_state_dirs_cmd+=" && mkdir -p '$STATE_ROOT/$slug' && chmod 700 '$STATE_ROOT/$slug'"
    # Group optional chown so mkdir/chmod failures still propagate; warn on chown miss.
    ensure_state_dirs_cmd+=" && { chown '$STATE_UID:$STATE_UID' '$STATE_ROOT/$slug' 2>/dev/null || echo \"warn: chown $STATE_UID failed for $STATE_ROOT/$slug\" >&2; }"
  done

  if is_local_host "$host"; then
    if ! {
      mkdir -p "$SHIM_DIR" &&
      install -m 755 "$HERE/himalaya-shim/run" "$SHIM_DIR/run" &&
      install -m 644 "$HERE/himalaya-shim/himalaya_config.py" "$SHIM_DIR/himalaya_config.py" &&
      install -m 755 "$CACHE_DIR/himalaya.${GARCH}" "$SHIM_DIR/himalaya" &&
      "$SHIM_DIR/himalaya" --version >/dev/null &&
      bash -c "$ensure_state_dirs_cmd"
    }; then
      echo "FAIL: local install failed" >&2
      fail=1
      continue
    fi
  else
    if ! {
      ssh "${SSH_OPTS[@]}" "$host" "mkdir -p '$SHIM_DIR'" &&
      rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$HERE/himalaya-shim/run" \
        "$HERE/himalaya-shim/himalaya_config.py" \
        "$host:$SHIM_DIR/" &&
      rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$CACHE_DIR/himalaya.${GARCH}" \
        "$host:$SHIM_DIR/himalaya" &&
      ssh "${SSH_OPTS[@]}" "$host" \
        "chmod 755 '$SHIM_DIR/run' '$SHIM_DIR/himalaya' && '$SHIM_DIR/himalaya' --version >/dev/null && $ensure_state_dirs_cmd"
    }; then
      echo "FAIL: remote install failed on $host" >&2
      fail=1
      continue
    fi
  fi
  echo "provisioned: $host"
done
[[ "$fail" -eq 0 ]] || exit 1
