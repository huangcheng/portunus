#!/usr/bin/env bash
# Provision the rclone connector shim on runner hosts:
#   deploy/provision-rclone-shim.sh [user@]host [...]
# For each host: installs the shim + version-pinned rclone binary (arch detected
# on the target via ssh) under /var/lib/portunus/skills/rclone-shim/, verifies
# the downloaded zip against the published SHA256SUMS, and appends the shim to
# that host's runner AND gateway policy binary_allowlist lists (compose paths).
# The rclone zip is cached under deploy/rclone-shim/ keyed by version.
#
# Downloads prefer GitHub Releases (downloads.rclone.org is often unreachable
# from domestic / some VPS egress). Exit 3 = skip (no usable download) so fleet
# sync can continue other components.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RCLONE_VERSION="${PORTUNUS_RCLONE_VERSION:-1.68.2}"
# Comma-separated fallbacks when the pinned version cannot be fetched.
RCLONE_VERSION_FALLBACKS="${PORTUNUS_RCLONE_VERSION_FALLBACKS:-1.68.2,1.67.0,1.65.2}"
SHIM_DIR="/var/lib/portunus/skills/rclone-shim"
SSH_OPTS=(-o ConnectTimeout=10)
# Writable cache (deploy/ is often bind-mounted :ro into the Temporal worker).
CACHE_DIR="${PORTUNUS_RCLONE_CACHE:-$HERE/rclone-shim}"
mkdir -p "$CACHE_DIR"
[[ $# -ge 1 ]] || { echo "usage: $0 [user@]host [...]" >&2; exit 2; }
for h in "$@"; do
  if [[ "$h" == -* ]]; then
    echo "unexpected option after hosts: $h (options must precede host names)" >&2
    exit 2
  fi
done
[[ -f "$HERE/rclone-shim/shim" ]] || { echo "missing $HERE/rclone-shim/shim" >&2; exit 2; }

rclone_urls() {
  local ver="$1" arch="$2"
  # Prefer GitHub; keep official CDN as secondary.
  echo "https://github.com/rclone/rclone/releases/download/v${ver}/rclone-v${ver}-linux-${arch}.zip"
  echo "https://downloads.rclone.org/rclone-v${ver}-linux-${arch}.zip"
}

rclone_sum_urls() {
  local ver="$1"
  echo "https://github.com/rclone/rclone/releases/download/v${ver}/SHA256SUMS"
  echo "https://downloads.rclone.org/v${ver}/SHA256SUMS"
}

fetch_rclone_amd64() {
  local ver="$1"
  local zip sums expected actual url
  for url in $(rclone_urls "$ver" amd64); do
    if curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 "$url" -o "$CACHE_DIR/rclone.zip"; then
      break
    fi
    rm -f "$CACHE_DIR/rclone.zip"
  done
  [[ -f "$CACHE_DIR/rclone.zip" ]] || return 1
  for url in $(rclone_sum_urls "$ver"); do
    if curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 "$url" -o "$CACHE_DIR/rclone.SHA256SUMS"; then
      break
    fi
    rm -f "$CACHE_DIR/rclone.SHA256SUMS"
  done
  [[ -f "$CACHE_DIR/rclone.SHA256SUMS" ]] || {
    echo "rclone SHA256SUMS unavailable for v${ver}" >&2
    rm -f "$CACHE_DIR/rclone.zip"
    return 1
  }
  expected="$(awk -v f="rclone-v${ver}-linux-amd64.zip" '$2 == f {print $1}' "$CACHE_DIR/rclone.SHA256SUMS")"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$CACHE_DIR/rclone.zip" | awk '{print $1}')"
  else
    actual="$(shasum -a 256 "$CACHE_DIR/rclone.zip" | awk '{print $1}')"
  fi
  [[ -n "$expected" && "$expected" == "$actual" ]] || {
    echo "rclone zip checksum mismatch for v${ver}" >&2
    rm -f "$CACHE_DIR/rclone.zip" "$CACHE_DIR/rclone.SHA256SUMS"
    return 1
  }
  unzip -q -o "$CACHE_DIR/rclone.zip" "rclone-v${ver}-linux-amd64/rclone" -d "$CACHE_DIR"
  mv "$CACHE_DIR/rclone-v${ver}-linux-amd64/rclone" "$CACHE_DIR/rclone"
  rm -rf "$CACHE_DIR/rclone.zip" "$CACHE_DIR/rclone-v${ver}-linux-amd64" "$CACHE_DIR/rclone.SHA256SUMS"
  printf '%s' "$ver" >"$CACHE_DIR/rclone.version"
  return 0
}

# Resolve a cached or freshly downloaded amd64 binary.
RESOLVED_VER=""
ver_out="$("$CACHE_DIR/rclone" version 2>/dev/null || true)"
if [[ -f "$CACHE_DIR/rclone" && -n "$ver_out" ]]; then
  # Reuse any cached binary that still runs (pin may have changed).
  RESOLVED_VER="$(tr -d '\n' <"$CACHE_DIR/rclone.version" 2>/dev/null || true)"
  if [[ -z "$RESOLVED_VER" ]]; then
    RESOLVED_VER="$(printf '%s\n' "$ver_out" | awk '/rclone v/{print $2; exit}' | sed 's/^v//')"
  fi
fi

if [[ -z "$RESOLVED_VER" || "$ver_out" != *"v${RCLONE_VERSION}"* ]]; then
  tried=()
  candidates=("$RCLONE_VERSION")
  IFS=',' read -r -a _fb <<<"$RCLONE_VERSION_FALLBACKS"
  for f in "${_fb[@]}"; do
    f="$(echo "$f" | tr -d '[:space:]')"
    [[ -n "$f" && "$f" != "$RCLONE_VERSION" ]] && candidates+=("$f")
  done
  for ver in "${candidates[@]}"; do
    tried+=("$ver")
    echo "rclone: trying v${ver}"
    if fetch_rclone_amd64 "$ver"; then
      RESOLVED_VER="$ver"
      RCLONE_VERSION="$ver"
      break
    fi
  done
  if [[ -z "$RESOLVED_VER" ]]; then
    echo "SKIP: rclone download unavailable (tried ${tried[*]})" >&2
    exit 3
  fi
else
  RCLONE_VERSION="$RESOLVED_VER"
fi

echo "rclone: using v${RCLONE_VERSION}"

for host in "$@"; do
  arch="$(ssh "${SSH_OPTS[@]}" "$host" 'uname -m')"
  case "$arch" in
    x86_64|amd64) RARCH=amd64 ;;
    aarch64|arm64) RARCH=arm64 ;;
    *) echo "unsupported arch on $host: $arch" >&2; exit 2 ;;
  esac
  # Child-uid state dir (rclone token conf + npm cache) - the shim creates it at
  # runtime as the child uid, which cannot mkdir under a root-owned skill-state
  # on hardened (drop-privs) deployments. See deploy/ensure-state-dirs.sh.
  ssh "${SSH_OPTS[@]}" "$host" "mkdir -p $SHIM_DIR /var/lib/portunus/skill-state/rclone && \
    chown -R 10001:10001 /var/lib/portunus/skill-state/rclone && \
    chmod 700 /var/lib/portunus/skill-state/rclone"
  if [[ "$RARCH" != "amd64" ]]; then
    # Per-arch artifact: fetched on the host into a private mktemp dir (fixed
    # world-writable /tmp paths would be a symlink-planting LPE), verified
    # against the version-pinned checksum manifest.
    if ! ssh "${SSH_OPTS[@]}" "$host" "set -e; d=\$(mktemp -d); \
      ok=0; \
      for url in $(rclone_urls "$RCLONE_VERSION" "$RARCH" | tr '\n' ' '); do \
        if curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 \"\$url\" -o \$d/rclone.zip; then ok=1; break; fi; \
      done; \
      [[ \$ok -eq 1 ]]; \
      ok=0; \
      for url in $(rclone_sum_urls "$RCLONE_VERSION" | tr '\n' ' '); do \
        if curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 \"\$url\" -o \$d/SHA256SUMS; then ok=1; break; fi; \
      done; \
      [[ \$ok -eq 1 ]]; \
      expected=\$(awk -v f='rclone-v${RCLONE_VERSION}-linux-${RARCH}.zip' '\$2 == f {print \$1}' \$d/SHA256SUMS) && \
      actual=\$(sha256sum \$d/rclone.zip | awk '{print \$1}') && \
      [[ -n \$expected && \$expected == \$actual ]] && \
      unzip -q -o \$d/rclone.zip -d \$d && \
      mv \$d/rclone-v${RCLONE_VERSION}-linux-${RARCH}/rclone $SHIM_DIR/rclone && \
      rm -rf \$d"; then
      echo "SKIP: rclone arm fetch failed on $host" >&2
      exit 3
    fi
  fi
  if [[ "$RARCH" == "amd64" ]]; then
    # amd64 uses the locally downloaded + checksum-verified artifact.
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$CACHE_DIR/rclone" "$host:$SHIM_DIR/rclone"
  fi
  rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/rclone-shim/shim" "$host:$SHIM_DIR/shim"
  ssh "${SSH_OPTS[@]}" "$host" "chmod 755 $SHIM_DIR/shim $SHIM_DIR/rclone && $SHIM_DIR/rclone version >/dev/null"
  # Policy: append to the runner and gateway policy lists on this host.
  ssh "${SSH_OPTS[@]}" "$host" "python3 - <<'PYEOF'
import json
import os
shim = '$SHIM_DIR/shim'
for p in (
    os.environ.get('PORTUNUS_POLICY', ''),
    '/etc/portunus/runner-policy.json',
    '/app/deploy/runner-policy.json',
    'deploy/runner-policy.json',
):
    if not p:
        continue
    try:
        with open(p) as f:
            d = json.load(f)
    except FileNotFoundError:
        continue
    except (OSError, ValueError) as e:
        print('skipping unreadable/malformed policy:', p, e)
        continue
    if shim not in d.get('binary_allowlist', []):
        d.setdefault('binary_allowlist', []).append(shim)
        # In-place write: the policy files are bind-mounted as single files into
        # the containers — a rename would swap the inode and leave the running
        # containers reading the old allowlist. Restart gateway+runners after.
        with open(p, 'w') as f:
            json.dump(d, f, indent=2)
        print('policy updated:', p)
PYEOF"
  echo "provisioned: $host"
done
