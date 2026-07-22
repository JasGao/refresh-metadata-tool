#!/bin/bash
# Commit and push a daily run log to the remote repo.
# Called by scripts/run_daily.sh after the workflow finishes.
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
LOG_FILE="${2:?log file required}"
EXIT_CODE="${3:-0}"
PYTHON_BIN="${4:-python3}"

if [ "${REFRESH_GIT_PUSH:-1}" = "0" ]; then
  echo "REFRESH_GIT_PUSH=0 — skipping git push"
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git not found — skipping log push"
  exit 0
fi

if ! git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "not a git repo — skipping log push"
  exit 0
fi

if [ ! -f "$LOG_FILE" ]; then
  echo "log file missing — skipping git push: $LOG_FILE"
  exit 0
fi

TOKEN_COUNT="$(
  "$PYTHON_BIN" -c "
import sys
sys.path.insert(0, '${REPO_DIR}')
from lib.tokenids import count_token_ids
print(count_token_ids())
" 2>/dev/null || echo 0
)"

LOG_BASENAME="$(basename "$LOG_FILE" .log)"
RUN_DATE=""
if [[ "$LOG_BASENAME" =~ ^daily-([0-9]{8})- ]]; then
  RUN_DATE="${BASH_REMATCH[1]}"
elif [[ "$LOG_BASENAME" =~ ^([0-9]{8})- ]]; then
  RUN_DATE="${BASH_REMATCH[1]}"
fi
if [ -z "$RUN_DATE" ]; then
  RUN_DATE="$(date +%Y%m%d)"
fi

if [ "$EXIT_CODE" -eq 0 ] || [ "$TOKEN_COUNT" -eq 0 ]; then
  RUN_STATUS="success"
else
  RUN_STATUS="failed"
fi

COMMIT_TITLE="${RUN_DATE}-${TOKEN_COUNT}rows-${RUN_STATUS}"
FINAL_LOG="$REPO_DIR/logs/${COMMIT_TITLE}.log"

if [ "$LOG_FILE" != "$FINAL_LOG" ]; then
  mv "$LOG_FILE" "$FINAL_LOG"
  LOG_FILE="$FINAL_LOG"
fi

rel_log="${LOG_FILE#"$REPO_DIR"/}"
git -C "$REPO_DIR" add -- "$rel_log"

if git -C "$REPO_DIR" diff --staged --quiet; then
  echo "no staged log changes — skipping commit"
  exit 0
fi

if ! git -C "$REPO_DIR" commit -m "$(cat <<EOF
${COMMIT_TITLE}
EOF
)"; then
  echo "git commit failed — skipping push"
  exit 0
fi

branch="$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || echo main)"

push_log() {
  git -C "$REPO_DIR" push origin "HEAD:refs/heads/$branch"
}

if push_log; then
  echo "pushed log to origin/$branch (${COMMIT_TITLE})"
elif [ -n "${GIT_SSH_COMMAND:-}" ]; then
  echo "git push failed on port 22 — retrying via ssh.github.com:443"
  export GIT_SSH_COMMAND="${GIT_SSH_COMMAND} -p 443 -o Hostname=ssh.github.com"
  if push_log; then
    echo "pushed log to origin/$branch (${COMMIT_TITLE}, via port 443)"
  else
    echo "git push failed — log committed locally only"
  fi
else
  echo "git push failed — log committed locally only"
fi
