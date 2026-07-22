#!/bin/bash
# Wrapper invoked by the macOS LaunchAgent for the daily BscScan refresh run.
#
# It normalizes the environment launchd provides (minimal PATH, no shell
# profile), exports the unattended-friendly BscScan tuning knobs, then runs
# run_workflow.py. Any arguments are passed straight through to the workflow
# (e.g. --crawl-only, --skip-reset).
#
# Manual test on the Mac:
#   scripts/run_daily.sh --crawl-only
set -uo pipefail

REPO_DIR="${REFRESH_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${REFRESH_PYTHON:-python3}"

# launchd starts jobs with a minimal PATH; add the usual Homebrew/system
# locations so python3, chromedriver, etc. resolve.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONUNBUFFERED=1
export HOME="${HOME:-$(/usr/bin/whoami | xargs -I{} /bin/sh -c 'eval echo ~{}')}"
export USER="${USER:-$(/usr/bin/whoami)}"

wait_for_network() {
  local max_wait="${REFRESH_NETWORK_WAIT:-90}"
  local waited=0
  while [ "$waited" -lt "$max_wait" ]; do
    if /usr/bin/host docs.google.com >/dev/null 2>&1 && /usr/bin/host github.com >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "WARNING: DNS not ready after ${max_wait}s — continuing anyway"
  return 1
}

setup_git_ssh() {
  local key="${HOME}/.ssh/id_ed25519"
  if [ ! -f "$key" ]; then
    return 0
  fi
  export GIT_SSH_COMMAND="ssh -i ${key} -o IdentitiesOnly=yes -o UserKnownHostsFile=${HOME}/.ssh/known_hosts"
}

# Unattended defaults (see README "OpenClaw / unattended cron"). Any value
# already set in the environment or scripts/schedule.local.env wins.
export BSCSCAN_CAPTCHA_WAIT="${BSCSCAN_CAPTCHA_WAIT:-120}"
export BSCSCAN_LOGIN_RETRIES="${BSCSCAN_LOGIN_RETRIES:-5}"
export BSCSCAN_CHROME_USER_DATA="${BSCSCAN_CHROME_USER_DATA:-$HOME/.refresh/chrome-bscscan}"
export BSCSCAN_CHROME_PROFILE="${BSCSCAN_CHROME_PROFILE:-Default}"

# Optional per-machine overrides (gitignored), e.g. BSCSCAN_ACCOUNT=refresh1
LOCAL_ENV="$REPO_DIR/scripts/schedule.local.env"
if [ -f "$LOCAL_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$LOCAL_ENV"
  set +a
fi

mkdir -p "$REPO_DIR/logs" "$BSCSCAN_CHROME_USER_DATA"
LOG="$REPO_DIR/logs/daily-$(date +%Y%m%d-%H%M%S).log"

cd "$REPO_DIR" || { echo "cannot cd to $REPO_DIR"; exit 1; }

# Stale chromedriver processes from crashed runs can leak file descriptors.
if command -v pgrep >/dev/null 2>&1; then
  while read -r pid; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done < <(pgrep -x chromedriver 2>/dev/null || true)
fi

wait_for_network || true
setup_git_ssh

code=0
{
  echo "=== refresh daily run: $(date) ==="
  echo "repo=$REPO_DIR"
  echo "python=$PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
  echo "args=${*:-<full pipeline>}"
  "$PYTHON_BIN" run_workflow.py "$@"
  code=$?
  echo "=== exit $code at $(date) ==="
} >>"$LOG" 2>&1

{
  echo "=== git push log: $(date) ==="
  bash "$REPO_DIR/scripts/push_daily_log.sh" "$REPO_DIR" "$LOG" "$code" "$PYTHON_BIN"
} >>"$LOG" 2>&1

exit $code
