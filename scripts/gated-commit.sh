#!/usr/bin/env bash
# Gated commit: every commit runs `ocr review --audience agent`, and the commit is
# REFUSED unless the findings are read and resolved. "ocr review && git commit" chains
# commit on the command's exit code, not on the findings — the mistake this prevents.
#
#   scripts/gated-commit.sh -m "subject" [-m "body ..."]        # zero findings required
#   scripts/gated-commit.sh -m "subject" --allow "why finding N is a false positive"
#
# Only -m/--allow are accepted here; staging flags (-a), --amend and friends are
# rejected on purpose: the gate must review exactly what is in the index.
set -euo pipefail
TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "gated-commit: not inside a git repository" >&2; exit 2; }
cd "$TOPLEVEL"

ARGS=() ; ALLOW=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m)
      [[ $# -ge 2 ]] || { echo "gated-commit: -m requires a value" >&2; exit 2; }
      ARGS+=("-m" "$2"); shift 2 ;;
    --allow)
      [[ $# -ge 2 ]] || { echo "gated-commit: --allow requires a value" >&2; exit 2; }
      [[ -z "${ALLOW_SET:-}" ]] || { echo "gated-commit: --allow given twice" >&2; exit 2; }
      [[ -n "$2" ]] || { echo "gated-commit: --allow requires a non-empty reason" >&2; exit 2; }
      ALLOW="$2"; ALLOW_SET=1; shift 2 ;;
    --)
      echo "gated-commit: rejecting '--' (only -m/--allow are accepted)" >&2; exit 2 ;;
    *)
      echo "gated-commit: rejecting option '$1' (stage with git add first; only -m/--allow are accepted)" >&2
      exit 2 ;;
  esac
done
[[ ${#ARGS[@]} -gt 0 ]] || { echo "usage: gated-commit.sh -m <subject> [-m <body>] [--allow <reason>]" >&2; exit 2; }
if git diff --cached --quiet; then
  echo "gated-commit: nothing staged" >&2; exit 2
fi

# Install the cleanup trap before any temp file exists, then freeze the index: the
# review must cover exactly what gets committed, with no window for a concurrent
# `git add` to slip unreviewed content into the commit.
OUT="$(mktemp "${TMPDIR:-/tmp}/ocr-gate.XXXXXX")"
IDX="$(mktemp "${TMPDIR:-/tmp}/ocr-gate-index.XXXXXX")"
trap 'rm -f "$OUT" "$IDX"' EXIT

IDX_ERR="$(mktemp "${TMPDIR:-/tmp}/ocr-gate-idxerr.XXXXXX")"
cp "$(git rev-parse --git-dir)/index" "$IDX"
export GIT_INDEX_FILE="$IDX"
if ! TREE_BEFORE="$(git write-tree 2>"$IDX_ERR")"; then
  echo "gated-commit: cannot read a committable index (unmerged entries? split index?)" >&2
  sed 's/^/  /' "$IDX_ERR" >&2 || true
  rm -f "$IDX_ERR"; exit 2
fi
rm -f "$IDX_ERR"
# ocr reads the working tree, not the frozen index: refuse when they diverge, or the
# review would cover content the commit does not contain (or miss content it does).
if ! git diff --quiet; then
  echo "gated-commit: REFUSING — the working tree has unstaged edits; ocr would review" >&2
  echo "content the commit does not contain. Stage or stash them first (git add / git stash)." >&2
  exit 1
fi
if ! ocr review --audience agent >"$OUT" 2>&1; then
  echo "gated-commit: ocr review itself failed — not committing. Output tail:" >&2
  tail -20 "$OUT" >&2
  exit 1
fi
# A *partial* review (files it could not read) is not a clean review: ocr exits 0 and
# reports 0 findings while having skipped content. Refuse on its error markers unless
# --allow explicitly records that the ocr malfunction itself is accepted (AGENTS.md:
# say so, never silently skip) — the marker lines are echoed into the commit body path.
OCR_ERRS="$(grep -E '^\[ocr\][[:space:]]+✘|^\[ocr\].*failed:' "$OUT" || true)"
if [[ -n "$OCR_ERRS" ]]; then
  if [[ -z "${ALLOW_SET:-}" ]] || ! grep -qi "ocr internal error" <<<"$ALLOW"; then
    echo "gated-commit: REFUSING — ocr review reported internal errors (incomplete review):" >&2
    printf "%s\n" "$OCR_ERRS" >&2
    echo 'To record an accepted ocr malfunction (AGENTS.md: say so, never silently skip):' >&2
    echo '  --allow "ocr internal error: <the lines above>"' >&2
    exit 1
  fi
  echo "== ocr internal errors accepted (recorded below) =="
  printf "%s\n" "$OCR_ERRS"
fi

# ocr may print intermediate "Review (partially) complete" lines; the FINAL summary is last.
COUNT="$(sed -nE 's/.*Review complete: ([0-9]+) finding\(s\).*/\1/p' "$OUT" | tail -1 || true)"
if [[ -z "$COUNT" ]]; then
  echo "gated-commit: could not parse a finding count (output format changed?) — not committing." >&2
  tail -20 "$OUT" >&2
  exit 1
fi
# A review that selected nothing while files are staged reviewed nothing.
SELECTED="$(sed -nE 's/.*Review (partially )?complete: [0-9]+ finding\(s\) across ([0-9]+) selected item\(s\).*/\2/p' "$OUT" | tail -1 || true)"
STAGED="$(git diff --cached --name-only | wc -l | tr -d ' ')"
if [[ "$SELECTED" == "0" && "$STAGED" != "0" ]]; then
  echo "gated-commit: REFUSING — ocr selected 0 items but $STAGED file(s) are staged (incomplete review)." >&2
  exit 1
fi
echo "== ocr review: $COUNT finding(s) =="
# Print the report verbatim: filtered headers hide the detail lines the reader needs.
cat "$OUT"

# The live index must still equal the reviewed tree (the frozen copy cannot change,
# so compare against the real one), then commit normally so hooks and the real index
# behave exactly as in a plain `git commit`.
LIVE_TREE="$(GIT_INDEX_FILE="$(git rev-parse --git-dir)/index" git write-tree)"
if [[ "$LIVE_TREE" != "$TREE_BEFORE" ]]; then
  echo "gated-commit: REFUSING — the index changed during the review. Re-run." >&2
  exit 1
fi
unset GIT_INDEX_FILE

if [[ "$COUNT" != "0" ]]; then
  if [[ -z "$ALLOW" ]]; then
    echo
    echo "gated-commit: REFUSING — $COUNT unresolved finding(s)." >&2
    echo "Resolve them, or pass --allow \"<one line per accepted finding>\" to record the judgment." >&2
    exit 1
  fi
  echo
  echo "Committing with recorded override: $ALLOW"
  TRAILER="ocr-gate: $COUNT finding(s) accepted — $ALLOW"
  if [[ -n "$OCR_ERRS" ]]; then
    TRAILER="$TRAILER

ocr internal error lines (accepted):
$OCR_ERRS"
  fi
  git commit "${ARGS[@]}" -m "$TRAILER"
else
  if [[ -n "$OCR_ERRS" ]]; then
    git commit "${ARGS[@]}" \
      -m "ocr-gate: accepted ocr malfunction ($COUNT findings) — $ALLOW" \
      -m "ocr internal error lines (accepted):
$OCR_ERRS"
  else
    git commit "${ARGS[@]}"
  fi
fi
