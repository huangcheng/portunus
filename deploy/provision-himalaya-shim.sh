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
SSH_OPTS=(-o ConnectTimeout=10)
CACHE_DIR="${PORTUNUS_HIMALAYA_CACHE:-$HERE/himalaya-shim}"
mkdir -p "$CACHE_DIR"
[[ $# -ge 1 ]] || { echo "usage: $0 [user@]host [...]" >&2; exit 2; }
[[ -f "$HERE/himalaya-shim/run" ]] || { echo "missing $HERE/himalaya-shim/run" >&2; exit 2; }
[[ -f "$HERE/himalaya-shim/himalaya_config.py" ]] || {
  echo "missing $HERE/himalaya-shim/himalaya_config.py" >&2
  exit 2
}

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

fail=0
for host in "$@"; do
  echo "=== himalaya-shim $host ==="
  arch_raw="$(ssh "${SSH_OPTS[@]}" "$host" 'uname -m')" || {
    echo "FAIL: cannot reach $host" >&2
    fail=1
    continue
  }
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
  ssh "${SSH_OPTS[@]}" "$host" "mkdir -p '$SHIM_DIR'"
  rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$HERE/himalaya-shim/run" \
    "$HERE/himalaya-shim/himalaya_config.py" \
    "$host:$SHIM_DIR/"
  rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$CACHE_DIR/himalaya.${GARCH}" \
    "$host:$SHIM_DIR/himalaya"
  ssh "${SSH_OPTS[@]}" "$host" "chmod 755 '$SHIM_DIR/run' '$SHIM_DIR/himalaya' && '$SHIM_DIR/himalaya' --version >/dev/null"
  echo "provisioned: $host"
done
[[ "$fail" -eq 0 ]] || exit 1
