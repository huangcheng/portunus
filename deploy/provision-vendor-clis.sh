#!/usr/bin/env bash
# Install vendor CLIs discovered from skill-bridge manifests (cli.provision).
#
# Ops host downloads (GitHub); each runner container gets binaries at the
# absolute paths declared in manifests. No per-app edits here — add
# ``cli.provision`` to a manifest and the next fleet sync picks it up.
#
#   deploy/provision-vendor-clis.sh [user@]host [...]
#
# Env:
#   PORTUNUS_VENDOR_CLI_CACHE   Writable cache (default /var/lib/portunus/cache/vendor-clis)
#   PORTUNUS_SKILL_BRIDGE_SRC   Manifest package root (optional)
#   PORTUNUS_RUNNER_CONTAINER   Override runner container name on each host
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SSH_OPTS=(-o ConnectTimeout=15 -o BatchMode=yes)
CACHE_DIR="${PORTUNUS_VENDOR_CLI_CACHE:-/var/lib/portunus/cache/vendor-clis}"
mkdir -p "$CACHE_DIR" || {
  echo "cannot create vendor-cli cache $CACHE_DIR (set PORTUNUS_VENDOR_CLI_CACHE to a writable dir)" >&2
  exit 2
}

[[ $# -ge 1 ]] || { echo "usage: $0 [user@]host [...]" >&2; exit 2; }
for h in "$@"; do
  if [[ "$h" == -* ]]; then
    echo "unexpected option after hosts: $h (options must precede host names)" >&2
    exit 2
  fi
done

DISCOVER="$HERE/discover-vendor-clis.py"
[[ -f "$DISCOVER" ]] || { echo "missing $DISCOVER" >&2; exit 2; }

# Prefer the skill_bridge venv (has PyYAML / pydantic); else system python3.
PYTHON=python3
for cand in \
  "${PORTUNUS_SKILL_BRIDGE_SRC:-}/.venv/bin/python" \
  "$HERE/../packages/portunus_skill_bridge/.venv/bin/python" \
  /app/packages/portunus_skill_bridge/.venv/bin/python \
  /app/gateway/.venv/bin/python
do
  if [[ -x "$cand" ]]; then
    PYTHON="$cand"
    break
  fi
done

# Keep discovery stderr visible (skip diagnostics); collect stdout as specs.
mapfile -t SPECS < <("$PYTHON" "$DISCOVER")
if [[ ${#SPECS[@]} -eq 0 ]]; then
  echo "provision-vendor-clis: no cli.provision entries in manifests (nothing to install)"
  exit 0
fi

echo "discovered ${#SPECS[@]} vendor CLI(s)"

fetch_github_release() {
  local repo="$1" tag="$2" asset_tmpl="$3" arch="$4" out="$5" member="$6"
  local asset="${asset_tmpl//\{arch\}/$arch}"
  if [[ -x "$out" ]]; then
    return 0
  fi
  local url="https://github.com/${repo}/releases/download/${tag}/${asset}"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/portunus-cli.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  echo "  fetch $url"
  # Note: called under `if !`; set -e is disabled — check curl/install explicitly.
  if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 180 -o "$tmp/asset" "$url"; then
    echo "download failed: $url" >&2
    return 1
  fi
  case "$asset" in
    *.tar.gz|*.tgz)
      tar -xzf "$tmp/asset" -C "$tmp" || return 1
      ;;
    *.zip)
      unzip -q -o "$tmp/asset" -d "$tmp" || return 1
      ;;
    *)
      # Raw binary asset (return install's status: set -e is disabled inside `if !`)
      install -m 755 "$tmp/asset" "$out"
      return
      ;;
  esac
  local found
  found="$(find "$tmp" -type f -name "$member" | head -1)"
  [[ -n "$found" ]] || { echo "archive missing $member" >&2; return 1; }
  install -m 755 "$found" "$out"
}

resolve_runner() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "$host" 'set -e
    C="${PORTUNUS_RUNNER_CONTAINER:-}"
    if [[ -z "$C" ]]; then
      C=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -E "portunus.*runner" | head -1 || true)
    fi
    if [[ -z "$C" ]]; then
      echo "FAIL: no runner container (set PORTUNUS_RUNNER_CONTAINER)" >&2
      exit 1
    fi
    printf "%s" "$C"
  '
}

fail=0
for host in "$@"; do
  echo "=== vendor-clis → $host ==="
  arch_raw="$(ssh "${SSH_OPTS[@]}" "$host" 'uname -m')" || {
    echo "FAIL: cannot reach $host" >&2
    fail=1
    continue
  }
  case "$arch_raw" in
    x86_64|amd64) RARCH=amd64 ;;
    aarch64|arm64) RARCH=arm64 ;;
    *)
      echo "FAIL: unsupported arch on $host: $arch_raw" >&2
      fail=1
      continue
      ;;
  esac

  runner="$(resolve_runner "$host")" || {
    echo "FAIL: resolve runner on $host" >&2
    fail=1
    continue
  }

  remote_tmp="/tmp/portunus-vendor-clis"
  if ! ssh "${SSH_OPTS[@]}" "$host" "mkdir -p '$remote_tmp'"; then
    echo "FAIL: mkdir remote tmp on $host" >&2
    fail=1
    continue
  fi

  for line in "${SPECS[@]}"; do
    # Parse once per record (avoid 7× python startups per host×spec).
    IFS=$'\t' read -r name dest kind repo tag asset binary < <(
      "$PYTHON" -c 'import json,sys
o=json.loads(sys.argv[1])
print("\t".join(str(o[k]) for k in ("name","dest","kind","repo","tag","asset","binary")))' "$line"
    )
    echo "  $name → $dest"
    [[ "$kind" == "github_release" ]] || {
      echo "FAIL: unsupported kind $kind for $name" >&2
      fail=1
      continue
    }
    cache_bin="$CACHE_DIR/${name}-${tag}-linux-${RARCH}"
    if ! fetch_github_release "$repo" "$tag" "$asset" "$RARCH" "$cache_bin" "$binary"; then
      echo "FAIL: download $name" >&2
      fail=1
      continue
    fi
    if ! rsync -az -e "ssh ${SSH_OPTS[*]}" "$cache_bin" "$host:$remote_tmp/$binary"; then
      echo "FAIL: upload $name to $host" >&2
      fail=1
      continue
    fi
    if ! ssh "${SSH_OPTS[@]}" "$host" "set -e
      docker cp '$remote_tmp/$binary' '$runner:$dest'
      docker exec '$runner' chmod 755 '$dest'
      docker exec '$runner' test -x '$dest'
      echo OK:$name:$runner
    "; then
      echo "FAIL: install $name into $runner on $host" >&2
      fail=1
      continue
    fi
  done
done

if [[ "$fail" -ne 0 ]]; then
  echo "provision-vendor-clis: one or more hosts failed" >&2
  exit 1
fi
echo "provision-vendor-clis: all hosts ok"
