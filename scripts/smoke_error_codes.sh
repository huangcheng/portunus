#!/usr/bin/env bash
# Post-deploy: assert the running gateway's ERROR_CODES match the repo frozenset.
# Usage: scripts/smoke_error_codes.sh [ssh-host]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-}"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3
  elif command -v python >/dev/null 2>&1; then PY=python
  else echo "python3 not found" >&2; exit 127; fi
fi

# Avoid shell-quote traps around \n / parentheses on remote zsh.
LIVE_PY=$'from portunus_shared.errors import ERROR_CODES\nprint(chr(10).join(sorted(ERROR_CODES)))'

read_live() {
  if [[ -n "$HOST" ]]; then
    ssh -o BatchMode=yes "$HOST" bash -s <<EOF
docker exec portunus-portunus-1 python -c $(printf '%q' "$LIVE_PY")
EOF
  else
    docker exec portunus-portunus-1 python -c "$LIVE_PY"
  fi
}

EXPECTED="$(
  cd "$ROOT/packages/portunus_shared"
  PYTHONPATH="$ROOT/packages/portunus_shared${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -c "$LIVE_PY"
)"
LIVE="$(read_live)"

if [[ "$EXPECTED" != "$LIVE" ]]; then
  echo "ERROR_CODES mismatch between repo and live gateway" >&2
  echo "--- repo ---" >&2
  echo "$EXPECTED" >&2
  echo "--- live ---" >&2
  echo "$LIVE" >&2
  diff -u <(printf '%s\n' "$EXPECTED") <(printf '%s\n' "$LIVE") >&2 || true
  exit 1
fi
echo "ERROR_CODES ok ($(printf '%s\n' "$EXPECTED" | grep -c . || true) codes)"
