#!/usr/bin/env python3
"""
Orchestrate the BscScan metadata diff + refresh workflow.

Flow:
  1. Update tokens.csv from the shared Google Sheet (CSV export)
  2. Ensure starting crawl account has a valid cookie
  3. Reset crawl/output/progress.json + crawl/output/report.json
  4. Run crawl/compare.py (lazy rotate/login on rate limit / Cloudflare)
  5. Progress + report updated by crawl
  6. Run refresh-metadata/refresh.py for outOfSync + crawl-error tokens (Selenium login + rotate on rate limit)

Usage:
  python3 run_workflow.py              # full pipeline
  python3 run_workflow.py --skip-login # use existing cookies (skip re-login)
  python3 run_workflow.py --skip-fetch # keep local tokens.csv (skip Google Sheet pull)
  python3 run_workflow.py --crawl-only # skip refresh
"""

import argparse
import fcntl
import os
import subprocess
import sys
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import accounts.login as login
from accounts.pool import AccountPool
from lib.fetch_tokens import fetch_tokens
from lib.log_util import banner, fail, info, kv, ok, step, substep, summary, warn
from lib.run_log import exit_code_from_system_exit, push_run_log, setup_run_log
from lib.paths import CRAWL_REPORT_FILE, TOKEN_IDS_FILE
from lib.report_tokens import refresh_target_counts
from lib.pool_config import account_env_for_refresh_tokens
from lib.reset_compare import reset_compare_files
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE, count_token_ids, refresh_cookies_needed

REPORT_FILE = CRAWL_REPORT_FILE
LOCK_FILE = os.path.join(SCRIPT_DIR, "crawl", "output", ".workflow.lock")
_lock_handle = None


def acquire_run_lock():
    """Refuse to start while another run is in progress.

    Two overlapping runs share the Chrome profiles, and the second run's
    stale-Chrome cleanup kills the first run's browser (2026-09-09: manual run
    overlapped the scheduled one and died with InvalidSessionIdException).
    """
    global _lock_handle
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        owner = handle.read().strip() or "unknown pid"
        handle.close()
        raise SystemExit(
            f"Another workflow run is already in progress (pid {owner}, lock {LOCK_FILE}). "
            "Wait for it to finish (or kill it) before starting a new run."
        )
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _lock_handle = handle


def run_phase(name, cmd, env=None):
    substep(name)
    # Force line-buffered output in the child so its lines reach the log as
    # they happen instead of in one burst at exit.
    merged = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}
    # sys.stdout is a tee into logs/ (see lib/run_log.py). A child that inherits
    # the raw terminal fd bypasses that tee, so every crawl/refresh line was
    # missing from the pushed logs. Pipe the child's output through the tee.
    proc = subprocess.Popen(
        cmd,
        cwd=SCRIPT_DIR,
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)


def print_inputs(token_count):
    summary("Run inputs", [
        ("Tokens in CSV", token_count),
    ])


def ensure_cookies(needed):
    pool = AccountPool()
    targets = pool.usernames()[:needed]
    if not targets:
        raise SystemExit("No accounts in accounts/accounts.json")

    kv("Account pool", ", ".join(targets))

    login_targets = [name for name in targets if pool.needs_crawl_login(name)]
    if not login_targets:
        ok(f"Cookies still valid today: {', '.join(targets)}")
        return

    for username in login_targets:
        entry = pool.session(username)
        for key in ("cookie", "userAgent", "crawlExhaustedUntil", "refreshExhaustedUntil"):
            entry.pop(key, None)
    pool.save()
    info(f"Re-logging in {len(login_targets)} account(s) via Selenium (steps 1→2→3)")

    token_id = login.first_token_id()
    info(f"First tokenId  {token_id}  (from tokens.csv)")
    for index, username in enumerate(login_targets):
        creds = pool.get_credentials(username)
        info(f"[{index + 1}/{len(login_targets)}] {username}")
        login.capture_account(pool, username, creds["password"], token_id, driver=None)
        if index + 1 < len(login_targets):
            time.sleep(login.ACCOUNT_GAP_SECONDS)


def parse_args():
    parser = argparse.ArgumentParser(description="Run the BscScan diff + refresh workflow.")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip step 1 (keep local tokens.csv, don't pull the Google Sheet)")
    parser.add_argument("--skip-login", action="store_true", help="Skip Selenium login (use existing cookies)")
    parser.add_argument("--skip-reset", action="store_true", help="Skip step 3 (resume crawl)")
    parser.add_argument("--crawl-only", action="store_true", help="Skip step 5 (refresh)")
    return parser.parse_args()


def main():
    args = parse_args()

    banner("BscScan Metadata Workflow")

    step(1, "Update tokens.csv from Google Sheet")
    if args.skip_fetch:
        warn("Skipped (--skip-fetch) — using existing tokens.csv")
    else:
        substep("Fetch shared token sheet (CSV export)")
        try:
            fetched, sheet_id, gid = fetch_tokens()
            if fetched == 0:
                ok(f"Sheet is empty (header only) — wrote tokens.csv from sheet {sheet_id} (gid={gid})")
            else:
                ok(f"Pulled {fetched} token ids from sheet {sheet_id} (gid={gid})")
        except Exception as exc:  # network / sharing / format failures
            fail(f"Sheet fetch failed: {exc}")
            # The sheet rarely changes between runs; a network blip should not
            # cost a whole day. Fall back to the last pulled copy if there is one.
            local_count = count_token_ids() if os.path.exists(TOKEN_IDS_FILE) else 0
            if local_count == 0:
                raise SystemExit("Aborting — could not refresh tokens.csv and no local copy to fall back on")
            warn(f"Using the local tokens.csv from the previous run ({local_count} token ids)")

    token_count = count_token_ids()
    print_inputs(token_count)

    if token_count == 0:
        ok("No token ids in tokens.csv — skipping crawl and refresh")
        banner("Workflow complete")
        return

    if not args.skip_login:
        step(2, "Ensure starting crawl cookie (account 1)")
        ensure_cookies(1)
    else:
        step(2, "Login accounts")
        warn("Skipped (--skip-login) — using existing cookies")

    if not args.skip_reset:
        step(3, "Reset compare output files")
        progress_file, report_file = reset_compare_files()
        ok(f"Progress reset: {os.path.basename(progress_file)}")
        ok(f"Report reset:   {os.path.basename(report_file)}")
    else:
        step(3, "Reset compare output files")
        warn("Skipped (--skip-reset) — resuming crawl")

    step(4, "Crawl — compare BscScan vs on-chain metadata")
    run_phase("crawl/compare.py", [sys.executable, "crawl/compare.py"])

    if args.crawl_only:
        banner("Done — refresh skipped (--crawl-only)")
        return

    refresh_counts = refresh_target_counts(REPORT_FILE)
    refresh_cookies = refresh_cookies_needed(refresh_counts["total"])

    step(5, "Refresh out-of-sync + crawl-error tokens (Selenium)")
    kv("Out-of-sync", refresh_counts["out_of_sync"])
    if refresh_counts["errors"]:
        kv("Crawl errors", refresh_counts["errors"])
    if refresh_counts.get("skipped"):
        kv("Not refreshable", f"{refresh_counts['skipped']} (tokenURI revert — token gone on-chain)")
    kv("To refresh", refresh_counts["total"])
    kv("Accounts needed", refresh_cookies)

    if refresh_counts["total"] == 0:
        ok("Nothing to refresh")
        banner("Workflow complete")
        return

    # Keep per-account refresh quota selection consistent with refresh.py.
    refresh_limit = int(os.environ.get("REFRESH_TOKENS_PER_COOKIE", str(REFRESH_TOKENS_PER_COOKIE)))
    refresh_env = account_env_for_refresh_tokens(refresh_counts["total"], limit=refresh_limit)
    kv("Account pool", refresh_env["BSCSCAN_ALLOWED_ACCOUNTS"])
    run_phase("refresh-metadata/refresh.py", [sys.executable, "refresh-metadata/refresh.py"], env=refresh_env)

    banner("Workflow complete")


if __name__ == "__main__":
    try:
        acquire_run_lock()
    except SystemExit as exc:
        print(exc.code, file=sys.stderr)
        sys.exit(2)
    setup_run_log()
    code = 0
    # Report the failure *before* push_run_log restores the streams — anything
    # the interpreter prints after that (tracebacks, SystemExit messages) only
    # reaches the terminal, never the pushed log.
    try:
        main()
    except SystemExit as exc:
        code = exit_code_from_system_exit(exc)
        if code != 0 and exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
    except KeyboardInterrupt:
        code = 130  # otherwise an interrupted run is pushed as "-success"
        print("Interrupted (KeyboardInterrupt)", file=sys.stderr)
    except Exception:
        code = 1
        traceback.print_exc()
    finally:
        push_run_log(code)
    sys.exit(code)
