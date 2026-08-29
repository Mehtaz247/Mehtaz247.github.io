#!/usr/bin/env bash
# One autonomous operating cycle for Overhead.
#
# Invoked by launchd on a schedule (see ops/install-schedule.sh). Hands the
# operator prompt to a headless Claude Code session with the repository as its
# working directory, then records what happened.
#
# Run manually with:  ops/cycle.sh            (normal cycle)
#                     ops/cycle.sh --dry-run  (plan only, no writes or pushes)

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd starts with a near-empty environment; node and claude live under nvm,
# so the PATH has to be reconstructed or the job silently does nothing.
export PATH="$HOME/.nvm/versions/node/v22.22.3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG_DIR="$REPO/ops/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d-%H%M)"
LOG="$LOG_DIR/cycle-$STAMP.log"
LOCK="$REPO/.cycle.lock"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# A cycle can outlive its interval. Overlapping runs would fight over the
# working tree and produce conflicting commits, so a stale-tolerant lock guards
# the whole thing.
if [ -d "$LOCK" ]; then
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
    echo "[$(date)] cycle already running (pid $(cat "$LOCK/pid")); exiting" | tee -a "$LOG"
    exit 0
  fi
  echo "[$(date)] clearing stale lock" | tee -a "$LOG"
  rm -rf "$LOCK"
fi
mkdir "$LOCK" 2>/dev/null || { echo "could not acquire lock"; exit 1; }
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

{
  echo "=============================================================="
  echo "cycle start: $(date)"
  echo "repo:        $REPO"
  echo "head:        $(git rev-parse --short HEAD 2>/dev/null || echo none)"
  echo "dry run:     $DRY_RUN"
  echo "=============================================================="
} | tee -a "$LOG"

# Start from a clean, current tree. A cycle that begins on top of uncommitted
# junk from a crashed previous run will commit that junk.
if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: working tree dirty at cycle start; stashing" | tee -a "$LOG"
  git stash push -u -m "auto-stash before cycle $STAMP" >>"$LOG" 2>&1
fi
git pull --rebase --autostash >>"$LOG" 2>&1 || echo "pull failed (offline?); continuing" | tee -a "$LOG"

PROMPT="$(cat "$REPO/ops/prompts/operator.md")"
if [ "$DRY_RUN" -eq 1 ]; then
  PROMPT="$PROMPT

## THIS RUN IS A DRY RUN
Decide what you would do and explain it in detail, but make no file changes, no
commits, and no pushes. End with the single action you would have taken."
fi

echo "invoking operator..." | tee -a "$LOG"
START=$(date +%s)

# --permission-mode bypassPermissions is required: there is no human present to
# answer a prompt, and a blocked cycle is a silent no-op. The blast radius is
# bounded by this being a dedicated repository with no credentials in it.
claude -p "$PROMPT" \
  --permission-mode bypassPermissions \
  --add-dir "$REPO" \
  >>"$LOG" 2>&1
STATUS=$?

ELAPSED=$(( $(date +%s) - START ))
echo "operator exited $STATUS after ${ELAPSED}s" | tee -a "$LOG"

# The operator is instructed to verify and push itself. This is the backstop for
# when it does not: never leave work stranded, but never push a red tree either.
if [ "$DRY_RUN" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
  echo "uncommitted changes remain; running verification before rescue commit" | tee -a "$LOG"
  if ops/verify.sh >>"$LOG" 2>&1; then
    git add -A
    git commit -m "ops: cycle $STAMP (rescue commit)" >>"$LOG" 2>&1
    git push >>"$LOG" 2>&1 && echo "rescue push ok" | tee -a "$LOG"
  else
    echo "VERIFY FAILED on leftover changes; reverting to last good commit" | tee -a "$LOG"
    git reset --hard HEAD >>"$LOG" 2>&1
    git clean -fd >>"$LOG" 2>&1
  fi
fi

echo "cycle end: $(date) (status $STATUS)" | tee -a "$LOG"

# Keep the last 40 logs. Unbounded growth in a repo that runs forever is a slow
# leak nobody would notice.
ls -1t "$LOG_DIR"/cycle-*.log 2>/dev/null | tail -n +41 | xargs rm -f 2>/dev/null

exit $STATUS
