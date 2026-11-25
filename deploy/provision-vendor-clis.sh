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

# Direct HTTPS file (Proton binary, or a .tar.gz/.zip whose member is ``binary``).
# ``{arch}`` → x64|arm64 (Proton). ``{linux_arch}`` → amd64|arm64.
# ``{linux_family}`` → amd|arm (Baidu CDN directory names).
fetch_http_file() {
  local url_tmpl="$1" arch="$2" out="$3" expect_sha="$4" member="$5"
  local parch="$arch" linux_arch="$arch" linux_family="$arch"
  case "$arch" in
    amd64) parch=x64; linux_arch=amd64; linux_family=amd ;;
    arm64) parch=arm64; linux_arch=arm64; linux_family=arm ;;
  esac
  local url="${url_tmpl//\{linux_family\}/$linux_family}"
  url="${url//\{linux_arch\}/$linux_arch}"
  url="${url//\{arch\}/$parch}"
  # Archive type is decided on the path only (signed URLs carry ?query tokens).
  local path_part="${url%%[?#]*}"
  local archive=""
  case "$path_part" in
    *.tar.gz|*.tgz) archive=tar ;;
    *.zip) archive=zip ;;
  esac
  # Archives: the cached member cannot be hashed against the archive digest, so a
  # stamp file records the verified digest. Raw binaries are re-hashed directly.
  local stamp="$out.sha512"
  if [[ -x "$out" ]]; then
    if [[ -z "$expect_sha" ]]; then
      echo "refusing to reuse $out without a sha512 pin" >&2
      return 1
    fi
    if [[ -z "$archive" ]]; then
      local got
      got="$(sha512sum "$out" | awk '{print $1}')"
      if [[ "$got" == "$expect_sha" ]]; then
        return 0
      fi
      echo "  cache checksum mismatch — re-fetch $url"
    elif [[ -f "$stamp" && "$(tr -d '[:space:]' <"$stamp")" == "$expect_sha" ]]; then
      return 0
    else
      echo "  cache stamp missing or mismatched — re-fetch $url"
    fi
  fi
  local tmp rc=0
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/portunus-cli.XXXXXX")" || return 1
  # Clean the temp dir on Ctrl-C / kill too, not only on the normal path.
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" INT TERM
  _fetch_http_file_install "$url" "$archive" "$tmp" "$out" "$stamp" "$expect_sha" "$member" || rc=$?
  rm -rf "$tmp"
  trap - INT TERM
  return "$rc"
}

# Download, verify, extract and install one http_file. Callers run this under
# `if !`, which disables set -e, so every step is checked explicitly.
_fetch_http_file_install() {
  local url="$1" archive="$2" tmp="$3" out="$4" stamp="$5" expect_sha="$6" member="$7"
  echo "  fetch $url"
  if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "$tmp/asset" "$url"; then
    echo "download failed: $url" >&2
    return 1
  fi
  local got_dl
  got_dl="$(sha512sum "$tmp/asset" | awk '{print $1}')"
  if [[ "$got_dl" != "$expect_sha" ]]; then
    echo "sha512 mismatch for $url" >&2
    echo "  expected $expect_sha" >&2
    echo "  got      $got_dl" >&2
    return 1
  fi
  case "$archive" in
    tar)
      tar -xzf "$tmp/asset" -C "$tmp" || return 1
      _install_archive_member "$tmp" "$member" "$out" || return 1
      printf '%s\n' "$expect_sha" >"$stamp" || return 1
      ;;
    zip)
      unzip -q -o "$tmp/asset" -d "$tmp" || return 1
      _install_archive_member "$tmp" "$member" "$out" || return 1
      printf '%s\n' "$expect_sha" >"$stamp" || return 1
      ;;
    *)
      install -m 755 "$tmp/asset" "$out" || return 1
      ;;
  esac
  return 0
}

# Pick exactly one file named $member from an extracted tree. Ambiguous matches
# (same basename in several places) fail instead of guessing.
_install_archive_member() {
  local tmp="$1" member="$2" out="$3"
  local -a matches=()
  local f
  while IFS= read -r f; do
    matches+=("$f")
  done < <(find "$tmp" -type f -name "$member" ! -path "$tmp/asset")
  if [[ ${#matches[@]} -eq 0 ]]; then
    echo "archive missing $member" >&2
    return 1
  fi
  if [[ ${#matches[@]} -gt 1 ]]; then
    echo "archive has ${#matches[@]} files named $member; refusing to guess" >&2
    printf '  %s\n' "${matches[@]}" >&2
    return 1
  fi
  install -m 755 "${matches[0]}" "$out" || return 1
  return 0
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
    "sha512_arm64="+q("sha512_arm64"),
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
        sha512_arm64="${sha512_arm64:-}"
        # sha512 is the amd64/x64 asset. arm64 needs sha512_arm64 when that pin is set.
        if [[ "$RARCH" == "arm64" && -n "$sha512" && -z "$sha512_arm64" ]]; then
          echo "FAIL: $name http_file sha512 is pinned to amd64/x64; set sha512_arm64" >&2
          fail=1
          continue
        fi
        if [[ -z "$url" ]]; then
          echo "FAIL: $name http_file missing url" >&2
          fail=1
          continue
        fi
        expect_sha="$sha512"
        if [[ "$RARCH" == "arm64" ]]; then
          expect_sha="$sha512_arm64"
        fi
        # Every http_file install must be pinned: never install unverified bytes.
        if [[ -z "$expect_sha" ]]; then
          echo "FAIL: $name http_file has no sha512 pin for $RARCH" >&2
          fail=1
          continue
        fi
        cache_bin="$CACHE_DIR/${name}-http-${RARCH}-${binary}"
        if ! fetch_http_file "$url" "$RARCH" "$cache_bin" "$expect_sha" "$binary"; then
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
