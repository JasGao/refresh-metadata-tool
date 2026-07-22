#!/usr/bin/env python3
"""
Refresh BscScan NFT metadata via Selenium (login + click refresh button).

For each out-of-sync or crawl-error token from the compare report:
  1. Log in with the active account (Selenium)
  2. Open the NFT page
  3. Click the Refresh Metadata button

Rotates accounts on rate limits, Cloudflare, or every REFRESH_TOKENS_PER_COOKIE tokens.
Exhausted accounts are marked in accounts/state.json until next UTC midnight.
Per-account daily quota is tracked in `refreshUsage` (startedAt / used / limit / remaining).
Same UTC day re-runs reuse saved cookies and continue the usage count.

Usage:
  python3 refresh-metadata/refresh.py
  python3 refresh-metadata/refresh.py --csv my-tokens.csv

Env:
  BSCSCAN_ACCOUNT       pin one account username (optional)
  BSCSCAN_REFRESH_DELAY seconds between tokens (default 5)
  REFRESH_TOKENS_PER_COOKIE rotate after this many tokens (default 100)
"""

import argparse
import os
import socket
import sys
import time

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import accounts.login as login
from accounts.pool import AccountPool, USAGE_REFRESH, cookie_has_auth
from lib.detect import is_cloudflare_html, is_rate_limited_text
from lib.log_util import banner, fail, info, kv, ok, short_token, summary, warn
from lib.pool_config import configure_pool_allowed
from lib.paths import CRAWL_REPORT_FILE, migrate_legacy_paths
from lib.report_tokens import load_refresh_token_ids, refresh_target_counts
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE

CONTRACT = login.CONTRACT
REPORT_FILE = CRAWL_REPORT_FILE
DELAY_SECONDS = float(os.environ.get("BSCSCAN_REFRESH_DELAY", "5"))
TOKENS_PER_ACCOUNT = int(os.environ.get("REFRESH_TOKENS_PER_COOKIE", str(REFRESH_TOKENS_PER_COOKIE)))
STEP_DELAY_SECONDS = login.STEP_DELAY_SECONDS

REFRESH_BUTTON = "#ContentPlaceHolder1_btnModalRefreshMetadata"
BROWSER_RESTART_ATTEMPTS = 2
CONNECTION_ERROR_MARKERS = (
    "connection refused",
    "connection reset",
    "chrome not reachable",
    "invalid session id",
    "failed to establish a new connection",
    "max retries exceeded",
    "read timed out",
    "httpconnectionpool",
)

pool = AccountPool()
active_account = None
driver = None


def load_tokens(report_path=REPORT_FILE, csv_path=None):
    if csv_path:
        with open(csv_path, "r") as file:
            tokens = [line.strip() for line in file if line.strip() and line.strip() != "tokenId"]
        return tokens

    if not os.path.exists(report_path):
        raise SystemExit(
            f"Report not found: {report_path}\nRun diff check first: python3 crawl/compare.py"
        )

    return load_refresh_token_ids(report_path)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Refresh BscScan metadata for out-of-sync and crawl-error tokens via Selenium."
    )
    parser.add_argument(
        "--report",
        default=REPORT_FILE,
        help="Compare report JSON (default: crawl/output/report.json)",
    )
    parser.add_argument(
        "--csv",
        help="Optional CSV of token IDs instead of reading the compare report",
    )
    parser.add_argument(
        "--from-token",
        help="Resume from this tokenId — accepts the full id or the …suffix shown in logs",
    )
    return parser.parse_args()


def slice_from_token(tokens, from_token):
    needle = from_token.replace("…", "").replace("...", "").strip()
    for index, token_id in enumerate(tokens):
        if token_id == needle or token_id.endswith(needle):
            return tokens[index:], index
    raise SystemExit(
        f"Token not found in refresh list: {from_token}\n"
        "Check the tokenId (or its suffix) against the report/CSV."
    )


def nft_url(token_id):
    return login.nft_page_url(token_id)


def is_browser_connection_error(error):
    """True only when Selenium lost contact with Chrome/chromedriver."""
    if isinstance(error, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, (TimeoutException, NoSuchElementException, StaleElementReferenceException)):
        return False

    try:
        from urllib3.exceptions import (
            ConnectTimeoutError,
            MaxRetryError,
            NewConnectionError,
            ReadTimeoutError,
        )

        if isinstance(error, (ReadTimeoutError, ConnectTimeoutError, NewConnectionError, MaxRetryError)):
            return True
    except ImportError:
        pass

    if isinstance(error, WebDriverException):
        message = str(error).lower()
        return any(marker in message for marker in CONNECTION_ERROR_MARKERS)

    message = str(error).lower()
    return any(marker in message for marker in CONNECTION_ERROR_MARKERS)


def is_cloudflare_page(driver):
    page = driver.page_source
    url = driver.current_url.lower()
    return bool(
        is_cloudflare_html(page)
        or "challenges.cloudflare.com" in url
        or "verify you are human" in page.lower()
        or ("troubleshoot" in page.lower() and "cloudflare" in page.lower())
    )


def is_rate_limited(driver):
    return (
        is_rate_limited_text(driver.page_source)
        or len(driver.find_elements(By.CSS_SELECTOR, "div.alert-danger")) > 0
    )


def log_pool_usage():
    rows = []
    for username in pool.pool_usernames():
        usage = pool.get_refresh_usage(username, limit=TOKENS_PER_ACCOUNT)
        if pool.is_exhausted(username, USAGE_REFRESH):
            status = "exhausted"
        else:
            started = usage.get("startedAt") or "not started"
            status = f"{usage['remaining']}/{usage['limit']} left (used {usage['used']}, since {started})"
        rows.append((username, status))
    summary("Refresh quota (today)", rows)


def parse_cookie_string(cookie_string):
    pairs = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        pairs.append((name.strip(), value.strip()))
    return pairs


def apply_cookies(browser, cookie_string):
    browser.get("https://bscscan.com")
    time.sleep(STEP_DELAY_SECONDS)
    login.dismiss_cookie_banner(browser)
    browser.delete_all_cookies()

    for name, value in parse_cookie_string(cookie_string):
        for domain in ("bscscan.com", ".bscscan.com"):
            try:
                browser.add_cookie({"name": name, "value": value, "domain": domain, "path": "/"})
                break
            except Exception:
                continue


def init_account():
    global active_account
    configure_pool_allowed(pool)
    pool.reset_to_first_allowed()
    allowed = pool.pool_usernames()
    pool.init_pool_refresh_usage(allowed, limit=TOKENS_PER_ACCOUNT)
    pin = os.environ.get("BSCSCAN_ACCOUNT")
    active_account = pool.get_active(USAGE_REFRESH, pin=pin, require_cookie=False)
    if not active_account:
        raise SystemExit(
            "No available accounts in pool."
            + (f" (pool: {', '.join(allowed)})" if allowed else "")
        )
    kv("Account pool", ", ".join(allowed))
    kv("Active account", active_account["username"])
    log_pool_usage()


def ensure_driver():
    global driver
    if driver is None:
        driver = login.create_driver()
    return driver


def quit_driver():
    global driver
    if driver is not None:
        login.terminate_driver(driver)
        driver = None


def _persist_session(browser, username):
    cookies = login.cookie_string(browser)
    user_agent = browser.execute_script("return navigator.userAgent") or ""
    pool.save_session(username, cookies, user_agent, clear_exhaustion=False)
    usage = pool.mark_refresh_usage_started(username, limit=TOKENS_PER_ACCOUNT)
    started = usage.get("startedAt", "")
    ok(
        f"Session ready  {username}  "
        f"({usage['remaining']}/{usage['limit']} left, started {started})"
    )


def ensure_driver_ready(token_id):
    """Ping chromedriver; restart and recover the profile session if it died."""
    global driver
    if driver is not None and login.driver_is_alive(driver):
        return driver
    warn("Chrome driver not responding — restarting")
    recover_browser_session(token_id)
    return ensure_driver()


def recover_browser_session(token_id):
    """Restart Chrome and prefer profile recovery over Turnstile login."""
    quit_driver()
    login.cleanup_stale_chromedrivers()
    browser = ensure_driver()
    username = active_account["username"]
    if login.try_recover_chrome_profile(browser, username, token_id=token_id):
        _persist_session(browser, username)
        return WebDriverWait(browser, 30)

    warn(f"Chrome profile session unavailable for {username} — full login required")
    login_active_account(token_id=token_id, force_full_login=True)
    return WebDriverWait(ensure_driver(), 30)


def try_saved_cookie_login(browser, username, token_id=None):
    cookie = pool.get_cookie(username)
    if not cookie or not cookie_has_auth(cookie):
        return False
    info(f"Trying saved cookies for {username} before Selenium login")
    apply_cookies(browser, cookie)
    if login.confirm_logged_in(browser):
        ok(f"Logged in via saved cookies  {username}")
        if token_id:
            login.visit_nft_page(browser, token_id)
        return True
    warn(f"Saved cookies did not restore session for {username}")
    return False


def navigate_browser(browser, url):
    browser.get(url)
    if login.is_browser_error_page(browser):
        raise RuntimeError("Browser connection error loading page")


def restart_browser_session(token_id):
    return recover_browser_session(token_id)


def refresh_token_with_browser_recovery(token_id, wait):
    last_error = None
    for attempt in range(1, BROWSER_RESTART_ATTEMPTS + 1):
        try:
            return refresh_token(token_id, wait)
        except Exception as error:
            if not is_browser_connection_error(error):
                raise
            last_error = error
            if attempt >= BROWSER_RESTART_ATTEMPTS:
                break
            warn(
                f"Browser error ({type(error).__name__}) — "
                f"restarting Chrome ({attempt}/{BROWSER_RESTART_ATTEMPTS})"
            )
            wait = restart_browser_session(token_id)
    return {
        "status": "error",
        "error": (
            f"Browser failed after {BROWSER_RESTART_ATTEMPTS} restarts — "
            f"{type(last_error).__name__}: {last_error}"
        ),
    }


def login_active_account(token_id=None, force_full_login=False):
    browser = ensure_driver()
    username = active_account["username"]
    password = active_account["password"]

    if not force_full_login:
        if login.try_recover_chrome_profile(browser, username, token_id=token_id):
            _persist_session(browser, username)
            return
        if try_saved_cookie_login(browser, username, token_id=token_id):
            _persist_session(browser, username)
            return

    info(f"Logging in  {username}")

    used_cookie_login = False
    for attempt in range(1, login.DRIVER_RETRIES + 1):
        try:
            if not login.driver_is_alive(browser):
                warn(f"Browser window closed — restarting Chrome ({attempt}/{login.DRIVER_RETRIES})")
                quit_driver()
                browser = ensure_driver()

            if not force_full_login and try_saved_cookie_login(browser, username, token_id=token_id):
                used_cookie_login = True
                break

            login.reset_browser_session(browser)
            login.selenium_login(browser, username, password)
            break
        except NoSuchWindowException:
            if attempt >= login.DRIVER_RETRIES:
                raise
            warn(f"Browser window closed — restarting Chrome ({attempt}/{login.DRIVER_RETRIES})")
            quit_driver()
            browser = ensure_driver()
    else:
        raise RuntimeError(f"Could not log in {username} — browser kept closing")

    if token_id and not used_cookie_login:
        login.visit_nft_page(browser, token_id)
    _persist_session(browser, username)


def ensure_active_account_session(token_id=None):
    username = active_account["username"]
    usage = pool.get_refresh_usage(username, limit=TOKENS_PER_ACCOUNT)

    if pool.can_reuse_refresh_session(username, limit=TOKENS_PER_ACCOUNT):
        pool.mark_refresh_usage_started(username, limit=TOKENS_PER_ACCOUNT)
        usage = pool.get_refresh_usage(username, limit=TOKENS_PER_ACCOUNT)
        started = usage.get("startedAt") or pool.session(username).get("lastLogin") or "unknown"
        info(
            f"Reusing today's session  {username}  "
            f"({usage['remaining']}/{usage['limit']} left, started {started})"
        )
        browser = ensure_driver()
        apply_cookies(browser, pool.get_cookie(username))
        if token_id:
            login.visit_nft_page(browser, token_id)
        ok(
            f"Session ready  {username}  "
            f"({usage['remaining']}/{usage['limit']} left, started {started})"
        )
        return

    browser = ensure_driver()
    if login.try_recover_chrome_profile(browser, username, token_id=token_id):
        _persist_session(browser, username)
        return

    login_active_account(token_id=token_id, force_full_login=True)


def rotate_account(mark_exhausted=None, warm_token_id=None):
    global active_account
    if mark_exhausted and active_account:
        pool.mark_exhausted(active_account["username"], mark_exhausted)

    next_account = pool.rotate(
        USAGE_REFRESH,
        active_account["username"] if active_account else None,
        require_cookie=False,
    )
    if not next_account:
        return None

    active_account = next_account
    warn(f"Rotated to {next_account['username']}")
    ensure_active_account_session(token_id=warm_token_id)
    return next_account


def note_token_processed():
    if not active_account:
        return
    usage = pool.note_refresh_used(active_account["username"], limit=TOKENS_PER_ACCOUNT)
    kv("Quota", f"{usage['remaining']}/{usage['limit']} remaining for {active_account['username']}")
    if usage["remaining"] <= 0:
        info("Refresh quota used up — rotating account")
        rotate_account(mark_exhausted=USAGE_REFRESH)


def ensure_active_quota():
    """Rotate immediately if current account has no quota left."""
    if not active_account:
        return
    usage = pool.get_refresh_usage(active_account["username"], limit=TOKENS_PER_ACCOUNT)
    if usage["remaining"] > 0:
        return
    info(f"Refresh quota already used up for {active_account['username']} — rotating account")
    if not rotate_account(mark_exhausted=USAGE_REFRESH):
        raise SystemExit("No available accounts in pool. (all refresh quotas exhausted)")


def click_refresh_metadata(driver, wait):
    """Open the NFT actions dropdown, then click Refresh Metadata."""
    clicked_dropdown = False

    # Prefer the NFT actions trigger; avoid topbar dropdowns like network selector.
    for selector in ("#ddOptionInvoker", "a#ddOptionInvoker", "button#ddOptionInvoker"):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                driver.execute_script("arguments[0].click();", element)
                clicked_dropdown = True
                break
        if clicked_dropdown:
            break

    # Fallback for layout variants: click a visible dropdown trigger except known topbar controls.
    if not clicked_dropdown:
        dropdowns = driver.find_elements(By.CSS_SELECTOR, "[data-bs-toggle='dropdown']")
        for element in reversed(dropdowns):
            element_id = (element.get_attribute("id") or "").strip()
            if element_id == "dropdownTopbarNetworks":
                continue
            if element.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                driver.execute_script("arguments[0].click();", element)
                clicked_dropdown = True
                break

    if not clicked_dropdown:
        raise RuntimeError("Could not find NFT actions dropdown trigger")

    time.sleep(1.5)

    # Target the actual <button> by id. BscScan wraps it in a
    # <span id="…btnModalRefreshMetadata_Title" onclick="event.stopPropagation();">,
    # and a text-matching XPath resolves to that span first (parent precedes
    # child in document order). Clicking the span is a silent no-op — the
    # button's __doPostBack never fires and no refresh is submitted.
    refresh_btn = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, REFRESH_BUTTON))
    )

    # The button is rendered disabled="true" until BscScan recognizes a
    # logged-in session; a JS click on a disabled button also does nothing.
    # Give client-side JS a moment to enable it, then fail loudly rather than
    # reporting a refresh that never happened.
    deadline = time.time() + 10
    while refresh_btn.get_attribute("disabled") and time.time() < deadline:
        time.sleep(0.5)
    if refresh_btn.get_attribute("disabled"):
        raise RuntimeError(
            "Refresh Metadata button stayed disabled — account not recognized as "
            "logged in on the NFT page (re-login needed)"
        )

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", refresh_btn)
    driver.execute_script("arguments[0].click();", refresh_btn)

    # The button triggers a plain ASP.NET __doPostBack (full-page form submit,
    # no UpdatePanel on this page), so a real submission reloads the page and
    # the old element goes stale. If it doesn't, the click was a no-op and the
    # refresh was NOT submitted — surface that instead of a false success.
    try:
        WebDriverWait(driver, 15).until(EC.staleness_of(refresh_btn))
    except TimeoutException:
        raise RuntimeError(
            "Refresh Metadata click did not trigger a postback (no-op) — refresh not submitted"
        )
    time.sleep(STEP_DELAY_SECONDS)


def refresh_token(token_id, wait, retries=0):
    # Retry on Cloudflare/rate-limits by rotating accounts.
    # Recursion replaced with a loop to avoid stack growth.
    max_retries = len(pool.pool_usernames())
    while True:
        if retries >= max_retries:
            account = active_account["username"] if active_account else "?"
            return {
                "status": "error",
                "error": (
                    f"All accounts failed for {account} — check login or add accounts. "
                    f"Re-login: python3 accounts/login.py --account {account}"
                ),
            }

        browser = ensure_driver_ready(token_id)
        url = nft_url(token_id)
        navigate_browser(browser, url)
        time.sleep(STEP_DELAY_SECONDS)
        login.dismiss_cookie_banner(browser)

        if is_cloudflare_page(browser):
            if not rotate_account(warm_token_id=token_id):
                account = active_account["username"] if active_account else "?"
                return {
                    "status": "error",
                    "error": (
                        f"Cloudflare blocked {account} — re-login: "
                        f"python3 accounts/login.py --account {account}"
                    ),
                }
            retries += 1
            continue

        if is_rate_limited(browser):
            if not rotate_account(mark_exhausted=USAGE_REFRESH, warm_token_id=token_id):
                return {"status": "rate_limited", "error": "Daily limit hit — no more accounts in pool"}
            retries += 1
            continue

        if not login.nft_page_ready(browser):
            if is_cloudflare_page(browser):
                if not rotate_account(warm_token_id=token_id):
                    account = active_account["username"] if active_account else "?"
                    return {
                        "status": "error",
                        "error": (
                            f"Cloudflare blocked {account} — re-login: "
                            f"python3 accounts/login.py --account {account}"
                        ),
                    }
                retries += 1
                continue
            return {
                "status": "error",
                "error": (
                    f"NFT page did not load for {active_account['username']}. "
                    f"Re-login: python3 accounts/login.py --account {active_account['username']}"
                ),
            }

        try:
            click_refresh_metadata(browser, wait)
        except TimeoutException as error:
            return {
                "status": "error",
                "error": f"Refresh Metadata button not found — {error}",
            }
        except Exception as error:
            if is_browser_connection_error(error):
                raise  # let caller restart Chrome and retry
            return {
                "status": "error",
                "error": f"Refresh Metadata button not found — {error}",
            }

        if is_cloudflare_page(browser):
            if not rotate_account(warm_token_id=token_id):
                account = active_account["username"] if active_account else "?"
                return {
                    "status": "error",
                    "error": (
                        f"Cloudflare blocked {account} after refresh click — re-login: "
                        f"python3 accounts/login.py --account {account}"
                    ),
                }
            retries += 1
            continue

        if is_rate_limited(browser):
            if not rotate_account(mark_exhausted=USAGE_REFRESH, warm_token_id=token_id):
                return {"status": "rate_limited", "error": "Daily limit hit — no more accounts in pool"}
            retries += 1
            continue

        return {"status": "ok"}


def main():
    migrate_legacy_paths()
    args = parse_args()
    tokens = load_tokens(report_path=args.report, csv_path=args.csv)

    banner("Refresh — Selenium")

    if not tokens:
        ok("No tokens to refresh (out-of-sync + crawl errors)")
        return

    skipped = 0
    if args.from_token:
        tokens, skipped = slice_from_token(tokens, args.from_token)

    source = args.csv or os.path.basename(args.report)
    rows = [("Source", source)]
    if skipped:
        rows.append(("Resume from", short_token(tokens[0])))
        rows.append(("Skipped (before)", skipped))
    if not args.csv:
        counts = refresh_target_counts(args.report)
        rows.append(("Out-of-sync", counts["out_of_sync"]))
        if counts["errors"]:
            rows.append(("Crawl errors", counts["errors"]))
    rows.extend([
        ("Tokens", len(tokens)),
        ("Limit per account", f"{TOKENS_PER_ACCOUNT} tokens"),
        ("Delay", f"{DELAY_SECONDS}s between tokens"),
    ])
    summary("Run config", rows)

    init_account()
    try:
        ensure_active_account_session(token_id=tokens[0])
        wait = WebDriverWait(ensure_driver(), 30)

        ok_count = fail_count = 0
        total = len(tokens)
        for index, token_id in enumerate(tokens):
            ensure_active_quota()
            current = index + 1
            label = short_token(token_id)
            info(f"Refreshing {label}  ({current}/{total})  (account: {active_account['username']})")
            result = refresh_token_with_browser_recovery(token_id, wait)

            if result["status"] == "ok":
                ok(f"{label}  refresh clicked")
                ok_count += 1
                note_token_processed()
            elif result["status"] == "rate_limited":
                fail(f"{label}  {result.get('error', 'rate limit')}")
                fail_count += 1
            else:
                fail(f"{label}  {result.get('error', result['status'])}")
                fail_count += 1

            if index + 1 < len(tokens):
                time.sleep(DELAY_SECONDS)

        summary("Refresh results", [
            ("Succeeded", ok_count),
            ("Failed", fail_count),
            ("Total", len(tokens)),
        ])
    finally:
        quit_driver()


if __name__ == "__main__":
    main()
