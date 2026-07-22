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
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import accounts.login as login
from accounts.pool import AccountPool
from lib.fetch_tokens import fetch_tokens
from lib.log_util import banner, fail, info, kv, ok, step, substep, summary, warn
from lib.paths import CRAWL_REPORT_FILE, migrate_legacy_paths
from lib.report_tokens import refresh_target_counts
from lib.pool_config import account_env_for_refresh_tokens
from lib.reset_compare import reset_compare_files
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE, count_token_ids, refresh_cookies_needed

REPORT_FILE = CRAWL_REPORT_FILE


def run_phase(name, cmd, env=None):
    substep(name)
    merged = {**os.environ, **(env or {})}
    subprocess.run(cmd, cwd=SCRIPT_DIR, env=merged, check=True)


def print_inputs(token_count):
    summary("Run inputs", [
        ("Tokens in CSV", token_count),
    ])


def ensure_cookies(needed, logged_accounts=None):
    pool = AccountPool()
    targets = pool.usernames()[:needed]
    if not targets:
        raise SystemExit("No accounts in accounts/accounts.json")

    kv("Account pool", ", ".join(targets))

    if logged_accounts is not None:
        login_targets = [
            name for name in targets
            if name not in logged_accounts and pool.needs_crawl_login(name)
        ]
        if not login_targets:
            ok(f"Cookies still valid today: {', '.join(targets)}")
            return logged_accounts
    else:
        login_targets = [name for name in targets if pool.needs_crawl_login(name)]
        if not login_targets:
            ok(f"Cookies still valid today: {', '.join(targets)}")
            return set(targets)

    for username in login_targets:
        entry = pool.session(username)
        entry.pop("cookie", None)
        entry.pop("userAgent", None)
        entry.pop("crawlExhaustedUntil", None)
        entry.pop("refreshGetExhaustedUntil", None)
        entry.pop("refreshPostExhaustedUntil", None)
        entry.pop("refreshExhaustedUntil", None)
        entry.pop("exhaustedUntil", None)
    pool.save()
    info(f"Re-logging in {len(login_targets)} account(s) via Selenium (steps 1→2→3)")

    token_id = login.first_token_id()
    info(f"First tokenId  {token_id}  (from tokens.csv)")
    try:
        for index, username in enumerate(login_targets):
            creds = pool.get_credentials(username)
            info(f"[{index + 1}/{len(login_targets)}] {username}")
            login.capture_account(pool, username, creds["password"], token_id, driver=None)
            if logged_accounts is not None:
                logged_accounts.add(username)
            if index + 1 < len(login_targets):
                time.sleep(login.ACCOUNT_GAP_SECONDS)
    finally:
        pass

    return logged_accounts or set(login_targets)


def parse_args():
    parser = argparse.ArgumentParser(description="Run the BscScan diff + refresh workflow.")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip step 1 (keep local tokens.csv, don't pull the Google Sheet)")
    parser.add_argument("--skip-login", action="store_true", help="Skip Selenium login (use existing cookies)")
    parser.add_argument("--skip-reset", action="store_true", help="Skip step 3 (resume crawl)")
    parser.add_argument("--crawl-only", action="store_true", help="Skip step 5 (refresh)")
    return parser.parse_args()


def main():
    migrate_legacy_paths()
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
            raise SystemExit("Aborting — could not refresh tokens.csv (use --skip-fetch to run on the local file)")

    token_count = count_token_ids()
    print_inputs(token_count)

    if token_count == 0:
        ok("No token ids in tokens.csv — skipping crawl and refresh")
        banner("Workflow complete")
        return

    logged_accounts = set()

    if not args.skip_login:
        step(2, "Ensure starting crawl cookie (account 1)")
        logged_accounts = ensure_cookies(1) or logged_accounts
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
    main()
