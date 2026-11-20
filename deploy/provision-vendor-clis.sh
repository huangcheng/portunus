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

# Capture discovery stdout and honor exit status (process substitution drops it).
SPECS_FILE="$(mktemp "${TMPDIR:-/tmp}/portunus-cli-specs.XXXXXX")"
# shellcheck disable=SC2064
trap 'rm -f "$SPECS_FILE"' EXIT
if ! "$PYTHON" "$DISCOVER" >"$SPECS_FILE"; then
  echo "provision-vendor-clis: manifest discovery failed (see stderr above)" >&2
  exit 1
fi
mapfile -t SPECS <"$SPECS_FILE"
rm -f "$SPECS_FILE"
trap - EXIT
if [[ ${#SPECS[@]} -eq 0 ]]; then
  echo "provision-vendor-clis: no cli.provision entries in manifests (nothing to install)"
  exit 0
fi

echo "discovered ${#SPECS[@]} vendor CLI(s)"

fetch_github_release() {
  local repo="$1" tag="$2" asset_tmpl="$3" arch="$4" out="$5" member="$6"
  local gnu_arch="$arch"
  case "$arch" in
    amd64) gnu_arch=x86_64 ;;
    arm64) gnu_arch=aarch64 ;;
  esac
  local asset="${asset_tmpl//\{gnu_arch\}/$gnu_arch}"
  asset="${asset//\{arch\}/$arch}"
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

# Direct HTTPS binary (e.g. Proton Drive CLI). ``{arch}`` → x64|arm64 (Proton) or amd64|arm64.
fetch_http_file() {
  local url_tmpl="$1" arch="$2" out="$3" expect_sha="$4"
  local parch="$arch"
  case "$arch" in
    amd64) parch=x64 ;;
    arm64) parch=arm64 ;;
  esac
  local url="${url_tmpl//\{arch\}/$parch}"
  if [[ -x "$out" ]]; then
    if [[ -n "$expect_sha" ]]; then
      local got
      got="$(sha512sum "$out" | awk '{print $1}')"
      if [[ "$got" == "$expect_sha" ]]; then
        return 0
      fi
      echo "  cache checksum mismatch — re-fetch $url"
    else
      return 0
    fi
  fi
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/portunus-cli.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  echo "  fetch $url"
  if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "$tmp/asset" "$url"; then
    echo "download failed: $url" >&2
    return 1
  fi
  if [[ -n "$expect_sha" ]]; then
    local got
    got="$(sha512sum "$tmp/asset" | awk '{print $1}')"
    if [[ "$got" != "$expect_sha" ]]; then
      echo "sha512 mismatch for $url" >&2
      echo "  expected $expect_sha" >&2
      echo "  got      $got" >&2
      return 1
    fi
  fi
  install -m 755 "$tmp/asset" "$out"
}

resolve_runner() {
  local host="$1"
  local override="${PORTUNUS_RUNNER_CONTAINER:-}"
  # Interpolate override into the remote command — ssh does not forward local env.
  ssh "${SSH_OPTS[@]}" "$host" "set -e
    C=$(printf '%q' "$override")
    if [[ -z \"\$C\" ]]; then
      C=\$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'portunus.*runner' | head -1 || true)
    fi
    if [[ -z \"\$C\" ]]; then
      echo 'FAIL: no runner container (set PORTUNUS_RUNNER_CONTAINER)' >&2
      exit 1
    fi
    printf '%s' \"\$C\"
  "
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

  remote_tmp="$(ssh "${SSH_OPTS[@]}" "$host" 'mktemp -d "${TMPDIR:-/tmp}/portunus-vendor-clis.XXXXXX"')" || {
    echo "FAIL: mkdtemp remote tmp on $host" >&2
    fail=1
    continue
  }

  for line in "${SPECS[@]}"; do
    # Parse via stdin JSON (argv breaks on long URLs / braces in some shells).
    eval "$("$PYTHON" -c 'import json,sys,shlex
o=json.load(sys.stdin)
def q(k):
    return shlex.quote(str(o.get(k) or ""))
print(
    "name="+q("name"),
    "dest="+q("dest"),
    "kind="+q("kind"),
    "repo="+q("repo"),
    "tag="+q("tag"),
    "asset="+q("asset"),
    "binary="+q("binary"),
    "url="+q("url"),
    "sha512="+q("sha512"),
    sep="; ",
)' <<<"$line")"
    echo "  $name → $dest ($kind)"
    cache_bin=""
    case "$kind" in
      github_release)
        cache_bin="$CACHE_DIR/${name}-${tag}-${asset//\//_}-linux-${RARCH}"
        if ! fetch_github_release "$repo" "$tag" "$asset" "$RARCH" "$cache_bin" "$binary"; then
          echo "FAIL: download $name" >&2
          fail=1
          continue
        fi
        ;;
      http_file)
        # Proton publishes linux-x64 checksums in the manifest; skip arm64 until dual hashes exist.
        if [[ "$RARCH" == "arm64" && -n "$sha512" ]]; then
          echo "FAIL: $name http_file sha512 is pinned to linux-x64; arm64 not configured" >&2
          fail=1
          continue
        fi
        if [[ -z "$url" ]]; then
          echo "FAIL: $name http_file missing url" >&2
          fail=1
          continue
        fi
        cache_bin="$CACHE_DIR/${name}-http-${RARCH}-${binary}"
        if ! fetch_http_file "$url" "$RARCH" "$cache_bin" "$sha512"; then
          echo "FAIL: download $name" >&2
          fail=1
          continue
        fi
        ;;
      *)
        echo "FAIL: unsupported kind $kind for $name" >&2
        fail=1
        continue
        ;;
    esac
    if ! rsync -az -e "ssh ${SSH_OPTS[*]}" "$cache_bin" "$host:$remote_tmp/$binary"; then
      echo "FAIL: upload $name to $host" >&2
      fail=1
      continue
    fi
    if ! ssh "${SSH_OPTS[@]}" "$host" "set -e
      docker cp '$remote_tmp/$binary' '$runner:$dest'
      docker exec '$runner' chmod 755 '$dest'
      docker exec '$runner' test -x '$dest'
      # Writable HOME/STATE_DIR for Go CLIs (drop-privs child uid).
      state='/var/lib/portunus/skill-state/$name'
      mkdir -p \"\$state\"
      chown ${PORTUNUS_STATE_UID:-10001}:${PORTUNUS_STATE_UID:-10001} \"\$state\" 2>/dev/null || true
      chmod 700 \"\$state\"
      echo OK:$name:$runner
    "; then
      echo "FAIL: install $name into $runner on $host" >&2
      fail=1
      continue
    fi
  done
  ssh "${SSH_OPTS[@]}" "$host" "rm -rf $(printf '%q' "$remote_tmp")" || true
done

if [[ "$fail" -ne 0 ]]; then
  echo "provision-vendor-clis: one or more hosts failed" >&2
  exit 1
fi
echo "provision-vendor-clis: all hosts ok"
