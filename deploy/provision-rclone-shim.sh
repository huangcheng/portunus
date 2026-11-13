#!/usr/bin/env bash
# Provision the rclone connector shim on runner hosts:
#   deploy/provision-rclone-shim.sh [user@]host [...]
# For each host: installs the shim + version-pinned rclone binary (arch detected
# on the target via ssh) under /var/lib/portunus/skills/rclone-shim/, verifies
# the downloaded zip against the published SHA256SUMS, and appends the shim to
# that host's runner AND gateway policy binary_allowlist lists (compose paths).
# The rclone zip is cached under deploy/rclone-shim/ keyed by version.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RCLONE_VERSION="1.76.0"
SHIM_DIR="/var/lib/portunus/skills/rclone-shim"
SSH_OPTS=(-o ConnectTimeout=10)
[[ $# -ge 1 ]] || { echo "usage: $0 [user@]host [...]" >&2; exit 2; }
[[ -f "$HERE/rclone-shim/shim" ]] || { echo "missing $HERE/rclone-shim/shim" >&2; exit 2; }

CACHE_OK=false
ver_out="$("$HERE/rclone-shim/rclone" version 2>/dev/null || true)"
if [[ -f "$HERE/rclone-shim/rclone" && "$ver_out" == *"v${RCLONE_VERSION}"* ]]; then
  CACHE_OK=true
fi
if [[ "$CACHE_OK" != "true" ]]; then
  curl -fsSL --retry 3 --max-time 300 \
    "https://downloads.rclone.org/rclone-v${RCLONE_VERSION}-linux-amd64.zip" \
    -o "$HERE/rclone.zip"
  curl -fsSL --retry 3 --max-time 60 \
    "https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS" -o "$HERE/rclone.SHA256SUMS"
  expected="$(awk -v f="rclone-v${RCLONE_VERSION}-linux-amd64.zip" '$2 == f {print $1}' "$HERE/rclone.SHA256SUMS")"
  actual="$(shasum -a 256 "$HERE/rclone.zip" | awk '{print $1}')"
  [[ -n "$expected" && "$expected" == "$actual" ]] || {
    echo "rclone zip checksum mismatch" >&2; exit 1;
  }
  unzip -q -o "$HERE/rclone.zip" "rclone-v${RCLONE_VERSION}-linux-amd64/rclone" -d "$HERE"
  mv "$HERE/rclone-v${RCLONE_VERSION}-linux-amd64/rclone" "$HERE/rclone-shim/rclone"
  rm -rf "$HERE/rclone.zip" "$HERE/rclone-v${RCLONE_VERSION}-linux-amd64" "$HERE/rclone.SHA256SUMS"
fi

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
    ssh "${SSH_OPTS[@]}" "$host" "set -e; d=\$(mktemp -d); \
      curl -fsSL --retry 3 --max-time 300 'https://downloads.rclone.org/rclone-v${RCLONE_VERSION}-linux-${RARCH}.zip' -o \$d/rclone.zip && \
      curl -fsSL --retry 3 --max-time 60 'https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS' -o \$d/SHA256SUMS && \
      expected=\$(awk -v f='rclone-v${RCLONE_VERSION}-linux-${RARCH}.zip' '\$2 == f {print \$1}' \$d/SHA256SUMS) && \
      actual=\$(sha256sum \$d/rclone.zip | awk '{print \$1}') && \
      [[ -n \$expected && \$expected == \$actual ]] && \
      unzip -q -o \$d/rclone.zip -d \$d && \
      mv \$d/rclone-v${RCLONE_VERSION}-linux-${RARCH}/rclone $SHIM_DIR/rclone && \
      rm -rf \$d"
  fi
  if [[ "$RARCH" == "amd64" ]]; then
    # amd64 uses the locally downloaded + checksum-verified artifact.
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/rclone-shim/rclone" "$host:$SHIM_DIR/rclone"
  fi
  rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/rclone-shim/shim" "$host:$SHIM_DIR/shim"
  ssh "${SSH_OPTS[@]}" "$host" "chmod 755 $SHIM_DIR/shim $SHIM_DIR/rclone && $SHIM_DIR/rclone version >/dev/null"
  # Policy: append to the runner and gateway policy lists on this host.
  ssh "${SSH_OPTS[@]}" "$host" "python3 - <<'PYEOF'
import json
import os
shim = '$SHIM_DIR/shim'
for p in ('/opt/stacks/portunus/runner-policy.json', '/opt/stacks/portunus/deploy/runner-policy.json'):
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
