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
from lib.paths import CRAWL_REPORT_FILE
from lib.report_tokens import load_refresh_token_ids, refresh_target_counts
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE

REPORT_FILE = CRAWL_REPORT_FILE
DELAY_SECONDS = float(os.environ.get("BSCSCAN_REFRESH_DELAY", "5"))
TOKENS_PER_ACCOUNT = int(os.environ.get("REFRESH_TOKENS_PER_COOKIE", str(REFRESH_TOKENS_PER_COOKIE)))
STEP_DELAY_SECONDS = login.STEP_DELAY_SECONDS

REFRESH_BUTTON = "#ContentPlaceHolder1_btnModalRefreshMetadata"
BROWSER_RESTART_ATTEMPTS = 2
NAVIGATE_ATTEMPTS = int(os.environ.get("BSCSCAN_NAVIGATE_ATTEMPTS", "2"))
DRIVER_RESTART_DELAY = float(os.environ.get("BSCSCAN_DRIVER_RESTART_DELAY", "3"))
BROWSER_RESTART_EVERY = int(os.environ.get("BSCSCAN_BROWSER_RESTART_EVERY", "18"))
# A failed rotation/login used to abort the whole run (2026-08-21, 09-04, 09-07,
# 09-08 all died that way with most tokens untouched). Now a token failure is
# recorded and the loop moves on; only a *streak* of failures stops the pass.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("BSCSCAN_MAX_CONSECUTIVE_FAILURES", "8"))
RETRY_FAILED_PASSES = int(os.environ.get("BSCSCAN_RETRY_FAILED_PASSES", "1"))
RETRY_FAILED_DELAY = float(os.environ.get("BSCSCAN_RETRY_FAILED_DELAY", "30"))
NETWORK_WAIT_SECONDS = float(os.environ.get("BSCSCAN_NETWORK_WAIT", "600"))
CONNECTION_ERROR_MARKERS = (
    "connection refused",
    "connection reset",
    "chrome not reachable",
    "invalid session id",
    "failed to establish a new connection",
    "max retries exceeded",
    "read timed out",
    "httpconnectionpool",
    "timed out receiving message from renderer",
)
PAGE_LOAD_TIMEOUT_MARKERS = (
    "timed out receiving message from renderer",
)
SESSION_LOST_MARKERS = (
    "refresh metadata button stayed disabled",
    "account not recognized as logged in",
)
LOGIN_FAILURE_MARKERS = (
    "login failed",
    "turnstile not ready",
    "nft page did not load",
    "could not log in",
)

pool = AccountPool()
active_account = None
driver = None
wait = None


def sync_wait():
    """Rebuild WebDriverWait after chromedriver is replaced."""
    global wait
    wait = WebDriverWait(ensure_driver(), 30)
    return wait


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


def is_page_load_timeout(error):
    if not isinstance(error, TimeoutException):
        return False
    message = str(error).lower()
    return any(marker in message for marker in PAGE_LOAD_TIMEOUT_MARKERS)


def is_browser_connection_error(error):
    """True only when Selenium lost contact with Chrome/chromedriver."""
    if isinstance(error, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if is_page_load_timeout(error):
        return True
    if isinstance(error, (NoSuchElementException, StaleElementReferenceException)):
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

    message = str(error).lower()
    return any(marker in message for marker in CONNECTION_ERROR_MARKERS)


def is_session_lost_error(error):
    message = str(error).lower()
    return any(marker in message for marker in SESSION_LOST_MARKERS)


def is_cloudflare_page(driver, page=None):
    if page is None:
        page = driver.page_source
    url = driver.current_url.lower()
    # Positive evidence wins: a page carrying the NFT properties block or the
    # ASP.NET form is the real page, whatever Cloudflare scripts it also loads.
    # Cloudflare's bot-management script keeps fetching from
    # /cdn-cgi/challenge-platform/... on healthy pages, so any marker-based test
    # on the rendered DOM misfires (2026-09-10: every token rotated accounts
    # twice and Refresh was never clicked).
    if 'id="collapseProperties"' in page or "__VIEWSTATE" in page:
        return False
    return bool(is_cloudflare_html(page) or "challenges.cloudflare.com" in url)


def network_up(timeout=5):
    try:
        socket.create_connection(("bscscan.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def wait_for_network(max_wait=NETWORK_WAIT_SECONDS):
    """Block until bscscan.com is reachable again (Wi-Fi drops mid-run happen)."""
    if network_up():
        return True
    warn(f"Network unreachable — waiting up to {int(max_wait)}s before continuing")
    end = time.monotonic() + max_wait
    while time.monotonic() < end:
        time.sleep(10)
        if network_up():
            ok("Network is back")
            return True
    fail(f"Network still unreachable after {int(max_wait)}s")
    return False


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
        username = active_account["username"] if active_account else None
        if username:
            login.set_active_chrome_user(username)
        driver = login.create_driver(username=username)
    return driver


def quit_driver():
    global driver
    if driver is not None:
        login.terminate_driver(driver)
        driver = None


def _persist_session(browser, username):
    cookies = login.capture_session_cookies(browser)
    user_agent = browser.execute_script("return navigator.userAgent") or ""
    pool.save_session(username, cookies, user_agent, clear_exhaustion=True)
    usage = pool.mark_refresh_usage_started(username, limit=TOKENS_PER_ACCOUNT)
    started = usage.get("startedAt", "")
    if not cookie_has_auth(cookies):
        warn(f"Saved cookie for {username} may not restore login after browser restart")
    ok(
        f"Session ready  {username}  "
        f"({usage['remaining']}/{usage['limit']} left, started {started})"
    )


def ensure_driver_ready(token_id):
    """Ping chromedriver; restart and recover the profile session if it died."""
    global driver, wait
    if driver is not None and login.driver_is_alive(driver):
        return driver
    warn("Chrome driver not responding — restarting")
    wait = recover_browser_session(token_id)
    return ensure_driver()


def _teardown_chrome(username):
    login.set_active_chrome_user(username)
    quit_driver()
    login.cleanup_stale_chrome_browsers(login.chrome_user_data_dir(username))
    login.cleanup_stale_chromedrivers()
    time.sleep(DRIVER_RESTART_DELAY)


def restore_browser_session(browser, username, token_id=None):
    """Restore a logged-in session after Chrome restart — profile, then cookies, then full login."""
    if login.try_recover_chrome_profile(browser, username, token_id=token_id):
        _persist_session(browser, username)
        return sync_wait()

    if try_saved_cookie_login(browser, username, token_id=token_id):
        _persist_session(browser, username)
        return sync_wait()

    warn(f"Saved session unavailable for {username} — full login required")
    login_active_account(token_id=token_id)
    return wait


def recover_browser_session(token_id):
    """Restart Chrome and prefer profile/cookie recovery over Turnstile login."""
    username = active_account["username"]
    _teardown_chrome(username)
    browser = ensure_driver()
    return restore_browser_session(browser, username, token_id=token_id)


def try_saved_cookie_login(browser, username, token_id=None):
    cookie = pool.get_cookie(username)
    if not cookie:
        return False
    if not cookie_has_auth(cookie):
        warn(f"Saved cookie for {username} is missing session markers")
        return False
    info(f"Trying saved cookies for {username} before Selenium login")
    try:
        apply_cookies(browser, cookie)
    except WebDriverException as error:
        warn(f"Could not apply saved cookies for {username} — {error}")
        return False
    if login.confirm_logged_in_as(browser, username):
        ok(f"Logged in via saved cookies  {username}")
        if token_id:
            try:
                login.visit_nft_page(browser, token_id)
            except RuntimeError:
                return False
        return True
    warn(f"Saved cookies did not restore session for {username}")
    return False


def navigate_browser(browser, url, attempts=NAVIGATE_ATTEMPTS):
    for attempt in range(1, attempts + 1):
        try:
            browser.get(url)
        except TimeoutException as error:
            if not is_page_load_timeout(error):
                raise
            warn(f"Page load timed out ({attempt}/{attempts}) — stopping load")
            try:
                browser.execute_script("window.stop();")
            except Exception:
                pass
            # Retrying the same URL is much cheaper than the Chrome restart + login
            # that a propagated browser error triggers.
            if attempt < attempts and not login.nft_page_ready(browser):
                continue
        if login.is_browser_error_page(browser):
            raise RuntimeError("Browser connection error loading page")
        return


def refresh_token_with_browser_recovery(token_id):
    global wait
    last_error = None
    for attempt in range(1, BROWSER_RESTART_ATTEMPTS + 1):
        try:
            return refresh_token(token_id)
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
            wait = recover_browser_session(token_id)
    return {
        "status": "error",
        "error": (
            f"Browser failed after {BROWSER_RESTART_ATTEMPTS} restarts — "
            f"{type(last_error).__name__}: {last_error}"
        ),
    }


def login_active_account(token_id=None):
    """Full Selenium login (Turnstile) for the active account."""
    browser = ensure_driver()
    username = active_account["username"]
    password = active_account["password"]
    info(f"Logging in  {username}")

    for attempt in range(1, login.DRIVER_RETRIES + 1):
        try:
            if not login.driver_is_alive(browser):
                warn(f"Browser window closed — restarting Chrome ({attempt}/{login.DRIVER_RETRIES})")
                quit_driver()
                browser = ensure_driver()

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

    if token_id:
        login.visit_nft_page(browser, token_id)
    _persist_session(browser, username)
    sync_wait()


def ensure_active_account_session(token_id=None):
    username = active_account["username"]

    if pool.can_reuse_refresh_session(username, limit=TOKENS_PER_ACCOUNT):
        pool.mark_refresh_usage_started(username, limit=TOKENS_PER_ACCOUNT)
        usage = pool.get_refresh_usage(username, limit=TOKENS_PER_ACCOUNT)
        started = usage.get("startedAt") or pool.session(username).get("lastLogin") or "unknown"
        info(
            f"Reusing today's session  {username}  "
            f"({usage['remaining']}/{usage['limit']} left, started {started})"
        )
        browser = ensure_driver()
        if try_saved_cookie_login(browser, username, token_id=token_id):
            ok(
                f"Session ready  {username}  "
                f"({usage['remaining']}/{usage['limit']} left, started {started})"
            )
            sync_wait()
            return
        warn(f"Saved session expired for {username} — restoring login")
        quit_driver()

    browser = ensure_driver()
    restore_browser_session(browser, username, token_id=token_id)


def switch_active_account_session(token_id=None):
    """Log out the current browser user and sign in as active_account."""
    username = active_account["username"]
    info(f"Switching browser session to {username}")
    _teardown_chrome(username)
    browser = ensure_driver()
    login.reset_browser_session(browser)
    return restore_browser_session(browser, username, token_id=token_id)


def rotate_account(mark_exhausted=None, warm_token_id=None, reason=""):
    global wait
    global active_account
    if mark_exhausted and active_account:
        pool.mark_exhausted(active_account["username"], mark_exhausted)
        reason = reason or "rate limit"

    next_account = pool.rotate(
        USAGE_REFRESH,
        active_account["username"] if active_account else None,
        require_cookie=False,
    )
    if not next_account:
        return None

    active_account = next_account
    warn(f"Rotated to {next_account['username']}" + (f" ({reason})" if reason else ""))
    wait = switch_active_account_session(token_id=warm_token_id)
    return next_account


def note_token_processed():
    if not active_account:
        return
    usage = pool.note_refresh_used(active_account["username"], limit=TOKENS_PER_ACCOUNT)
    kv("Quota", f"{usage['remaining']}/{usage['limit']} remaining for {active_account['username']}")
    if usage["remaining"] <= 0:
        info("Refresh quota used up — rotating account")
        rotate_account(reason="quota used up")


def ensure_active_quota():
    """Rotate immediately if current account has no quota left."""
    if not active_account:
        return
    usage = pool.get_refresh_usage(active_account["username"], limit=TOKENS_PER_ACCOUNT)
    if usage["remaining"] > 0:
        return
    info(f"Refresh quota already used up for {active_account['username']} — rotating account")
    if not rotate_account(reason="quota used up"):
        raise SystemExit("No available accounts in pool. (all refresh quotas exhausted)")


def _cloudflare_failure(when=""):
    account = active_account["username"] if active_account else "?"
    suffix = f" {when}".rstrip()
    return {
        "status": "error",
        "error": (
            f"Cloudflare blocked {account}{suffix} — re-login: "
            f"python3 accounts/login.py --account {account}"
        ),
    }


def _rotate_on_cloudflare(warm_token_id, when=""):
    reason = f"Cloudflare challenge {when}".strip()
    if rotate_account(warm_token_id=warm_token_id, reason=reason):
        return None
    return _cloudflare_failure(when)


def click_refresh_metadata(driver):
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
    banner = postback_banner_text(driver)
    if banner:
        info(f"BscScan says: {banner}", indent=4)


def postback_banner_text(driver):
    """Text of the alert BscScan shows after the refresh postback (for the log)."""
    try:
        for element in driver.find_elements(By.CSS_SELECTOR, ".alert, [role='alert'], .toast-body"):
            text = " ".join((element.text or "").split())
            if text and element.is_displayed():
                return text[:160]
    except Exception:
        pass
    return ""


def refresh_token(token_id, retries=0, session_retries=0):
    global wait
    # Retry on Cloudflare/rate-limits by rotating accounts.
    # Recursion replaced with a loop to avoid stack growth.
    max_retries = len(pool.pool_usernames())
    max_session_retries = 2
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
        navigate_browser(browser, login.nft_page_url(token_id))
        time.sleep(STEP_DELAY_SECONDS)
        login.dismiss_cookie_banner(browser)

        # One page_source round trip (~150KB) serves all three checks below.
        page = browser.page_source
        if is_cloudflare_page(browser, page=page):
            blocked = _rotate_on_cloudflare(token_id)
            if blocked:
                return blocked
            retries += 1
            continue

        if is_rate_limited_text(page):
            if not rotate_account(mark_exhausted=USAGE_REFRESH, warm_token_id=token_id):
                return {"status": "rate_limited", "error": "Daily limit hit — no more accounts in pool"}
            retries += 1
            continue

        if not login.nft_page_ready(browser, page=page):
            return {
                "status": "error",
                "error": (
                    f"NFT page did not load for {active_account['username']}. "
                    f"Re-login: python3 accounts/login.py --account {active_account['username']}"
                ),
            }

        if not login.is_logged_in(browser):
            if session_retries >= max_session_retries:
                return {
                    "status": "error",
                    "error": (
                        f"Session lost for {active_account['username']} — "
                        f"re-login: python3 accounts/login.py --account {active_account['username']}"
                    ),
                }
            session_retries += 1
            warn(f"Not logged in — restoring session for {active_account['username']}")
            wait = recover_browser_session(token_id)
            continue

        try:
            click_refresh_metadata(browser)
        except TimeoutException as error:
            if is_browser_connection_error(error):
                raise  # renderer hang: let the caller restart Chrome and retry
            return {
                "status": "error",
                "error": f"Refresh Metadata button not found — {error}",
            }
        except RuntimeError as error:
            if is_session_lost_error(error):
                if session_retries >= max_session_retries:
                    return {
                        "status": "error",
                        "error": (
                            f"Session lost for {active_account['username']} — "
                            f"re-login: python3 accounts/login.py --account {active_account['username']}"
                        ),
                    }
                session_retries += 1
                warn(f"Session lost — restoring login for {active_account['username']}")
                wait = recover_browser_session(token_id)
                continue
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

        page = browser.page_source
        if is_cloudflare_page(browser, page=page):
            blocked = _rotate_on_cloudflare(token_id, when="after refresh click")
            if blocked:
                return blocked
            retries += 1
            continue

        if is_rate_limited_text(page):
            if not rotate_account(mark_exhausted=USAGE_REFRESH, warm_token_id=token_id):
                return {"status": "rate_limited", "error": "Daily limit hit — no more accounts in pool"}
            retries += 1
            continue

        return {"status": "ok"}


def first_line(text):
    return str(text).strip().splitlines()[0] if str(text).strip() else ""


def is_login_failure(error):
    message = str(error).lower()
    return any(marker in message for marker in LOGIN_FAILURE_MARKERS)


def process_token(token_id):
    """Refresh one token and never raise: any error becomes a failed result."""
    try:
        return refresh_token_with_browser_recovery(token_id)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        # Rotation, login and navigation errors used to escape here and kill the
        # run. Drop the browser so the next token starts from a clean recovery.
        warn(f"Unexpected error on {short_token(token_id)} — {type(error).__name__}: {first_line(error)}")
        quit_driver()
        if is_login_failure(error):
            # Don't hammer the account that just failed Turnstile; move on and
            # let the quota logic come back to it later.
            try:
                rotate_account(reason="login failed", warm_token_id=token_id)
            except KeyboardInterrupt:
                raise
            except Exception as rotate_error:
                warn(f"Rotation after login failure also failed — {type(rotate_error).__name__}: {first_line(rotate_error)}")
                quit_driver()
        return {"status": "error", "error": f"{type(error).__name__}: {first_line(error)}"}


def proactive_restart(next_token_id):
    try:
        recover_browser_session(next_token_id)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        warn(f"Proactive restart failed — {type(error).__name__}: {first_line(error)}; will recover on the next token")
        quit_driver()


def run_pass(tokens, pass_label=""):
    """Refresh every token once. Returns (ok_count, failed[(token, error)], skipped[tokens])."""
    ok_count = 0
    failed = []
    consecutive_failures = 0
    tokens_since_browser_restart = 0
    total = len(tokens)

    for index, token_id in enumerate(tokens):
        try:
            ensure_active_quota()
        except SystemExit as exc:
            fail(str(exc))
            return ok_count, failed, tokens[index:]

        current = index + 1
        label = short_token(token_id)
        info(f"Refreshing {label}  ({current}/{total}){pass_label}  (account: {active_account['username']})")
        token_started = time.monotonic()
        result = process_token(token_id)
        elapsed = time.monotonic() - token_started

        if result["status"] == "ok":
            ok(f"{label}  refresh clicked  ({elapsed:.1f}s)")
            ok_count += 1
            consecutive_failures = 0
            note_token_processed()
            tokens_since_browser_restart += 1
            if (
                BROWSER_RESTART_EVERY > 0
                and tokens_since_browser_restart >= BROWSER_RESTART_EVERY
                and index + 1 < len(tokens)
            ):
                info(f"Proactive browser restart after {tokens_since_browser_restart} tokens")
                proactive_restart(tokens[index + 1])
                tokens_since_browser_restart = 0
        else:
            error = result.get("error", result["status"])
            fail(f"{label}  ({elapsed:.1f}s)  {error}")
            failed.append((token_id, error))
            consecutive_failures += 1
            if result["status"] == "rate_limited" and "no more accounts" in error:
                fail("Every account is out of refresh quota — stopping this pass")
                return ok_count, failed, tokens[index + 1:]
            if consecutive_failures >= 2 and not network_up():
                if not wait_for_network():
                    return ok_count, failed, tokens[index + 1:]
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                fail(
                    f"{consecutive_failures} consecutive failures — stopping this pass "
                    f"({len(tokens) - current} tokens not attempted)"
                )
                return ok_count, failed, tokens[index + 1:]

        if index + 1 < len(tokens):
            time.sleep(DELAY_SECONDS)

    return ok_count, failed, []


def main():
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
        if counts.get("skipped"):
            rows.append(("Not refreshable", f"{counts['skipped']} (tokenURI revert)"))
    rows.extend([
        ("Tokens", len(tokens)),
        ("Limit per account", f"{TOKENS_PER_ACCOUNT} tokens"),
        ("Delay", f"{DELAY_SECONDS}s between tokens"),
        ("Retry passes", RETRY_FAILED_PASSES),
    ])
    summary("Run config", rows)

    init_account()
    aborted = False
    try:
        try:
            ensure_active_account_session(token_id=tokens[0])  # every path ends in sync_wait()
        except Exception as error:
            # Recoverable per token: process_token re-creates the session lazily.
            warn(f"Initial session setup failed — {type(error).__name__}: {first_line(error)}; retrying per token")
            quit_driver()

        ok_count, failed, not_attempted = run_pass(tokens)
        aborted = bool(not_attempted)

        for pass_number in range(1, RETRY_FAILED_PASSES + 1):
            if not failed or aborted:
                break
            info(f"Retry pass {pass_number}/{RETRY_FAILED_PASSES}: {len(failed)} failed token(s) after {RETRY_FAILED_DELAY:.0f}s")
            time.sleep(RETRY_FAILED_DELAY)
            quit_driver()  # fresh Chrome for the retry
            retry_ok, failed, not_attempted = run_pass(
                [token_id for token_id, _ in failed], pass_label=f"  [retry {pass_number}]"
            )
            ok_count += retry_ok
            aborted = bool(not_attempted)

        summary("Refresh results", [
            ("Succeeded", ok_count),
            ("Failed", len(failed)),
            ("Not attempted", len(not_attempted)),
            ("Total", len(tokens)),
        ])
        if failed:
            info("Failed tokens:")
            for token_id, error in failed:
                fail(f"{short_token(token_id)}  {error}", indent=4)
    finally:
        quit_driver()

    if aborted:
        raise SystemExit("Refresh stopped early — see 'Not attempted' above")


if __name__ == "__main__":
    main()
