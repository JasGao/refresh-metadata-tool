import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time

import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from accounts.pool import AccountPool, AUTH_COOKIE_MARKERS, DEFAULT_USER_AGENT
from lib.detect import is_cloudflare_html
from lib.log_util import info, ok, warn
from lib.report_tokens import refresh_target_counts
from lib.tokenids import count_token_ids, cookies_needed, load_token_ids, refresh_cookies_needed, TOKENS_PER_COOKIE, REFRESH_TOKENS_PER_COOKIE

CONTRACT = os.environ.get("BSCSCAN_CONTRACT", "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30")
LOGIN_URL = "https://bscscan.com/login"
TYPE_DELAY_SECONDS = float(os.environ.get("BSCSCAN_LOGIN_TYPE_DELAY", "0.25"))
STEP_DELAY_SECONDS = float(os.environ.get("BSCSCAN_LOGIN_STEP_DELAY", "2.5"))
ACCOUNT_GAP_SECONDS = float(os.environ.get("BSCSCAN_LOGIN_ACCOUNT_GAP", "5"))
CAPTCHA_WAIT_SECONDS = float(os.environ.get("BSCSCAN_CAPTCHA_WAIT", "60"))
LOGIN_RETRIES = int(os.environ.get("BSCSCAN_LOGIN_RETRIES", "3"))
POST_TURNSTILE_DELAY = float(os.environ.get("BSCSCAN_POST_TURNSTILE_DELAY", "3"))
LOGIN_SUBMIT_WAIT = float(os.environ.get("BSCSCAN_LOGIN_SUBMIT_WAIT", "3"))
PAGE_LOAD_TIMEOUT = int(os.environ.get("BSCSCAN_PAGE_LOAD_TIMEOUT", "35"))
# Selenium 4 defaults the HTTP wait on chromedriver to None (block forever), so a
# wedged renderer hangs the run silently. Bound it.
COMMAND_TIMEOUT = int(os.environ.get("BSCSCAN_COMMAND_TIMEOUT", "90"))
# "eager" returns at DOMContentLoaded — BscScan pages keep loading ads/trackers long
# after the refresh button is usable. Set to "normal" to restore the old behaviour.
PAGE_LOAD_STRATEGY = os.environ.get("BSCSCAN_PAGE_LOAD_STRATEGY", "eager").strip().lower()
STRICT_USERNAME_CHECK = os.environ.get("BSCSCAN_STRICT_USERNAME", "").strip().lower() in ("1", "true", "yes")
# If no Turnstile widget has rendered after this many seconds, none is coming —
# polling out the rest of BSCSCAN_CAPTCHA_WAIT just burns wall clock.
TURNSTILE_ABSENT_GRACE = float(os.environ.get("BSCSCAN_TURNSTILE_ABSENT_GRACE", "12"))
MANUAL_LOGIN = os.environ.get("BSCSCAN_MANUAL_LOGIN", "").strip().lower() in ("1", "true", "yes")
MYACCOUNT_URL = "https://bscscan.com/myaccount"


def first_token_id():
    token_ids = load_token_ids()
    return token_ids[0] if token_ids else "1"


DRIVER_RETRIES = int(os.environ.get("BSCSCAN_DRIVER_RETRIES", "3"))
DRIVER_RETRY_DELAY = float(os.environ.get("BSCSCAN_DRIVER_RETRY_DELAY", "5"))
# undetected_chromedriver's default mode deletes its cached chromedriver and
# re-downloads + re-patches an ~18MB binary on EVERY browser start, using
# urlopen/urlretrieve with no timeout. On a flaky network that hangs forever
# right after the "Chrome version_main" log line, with no browser window.
# Reuse the cached, already-patched binary (user_multi_procs=True) and only
# download when the cache is empty or no longer matches Chrome.
DRIVER_DOWNLOAD_TIMEOUT = float(os.environ.get("BSCSCAN_DRIVER_DOWNLOAD_TIMEOUT", "120"))
CHROME_USER_DATA = os.environ.get(
    "BSCSCAN_CHROME_USER_DATA",
    os.path.expanduser("~/.refresh/chrome-bscscan"),
).strip()
CHROME_PROFILE = os.environ.get("BSCSCAN_CHROME_PROFILE", "Default").strip() or "Default"
_active_chrome_user = None


def set_active_chrome_user(username):
    global _active_chrome_user
    _active_chrome_user = username


def chrome_user_data_dir(username=None):
    username = username or _active_chrome_user
    if not CHROME_USER_DATA:
        return ""
    if username:
        return os.path.join(CHROME_USER_DATA, "profiles", username)
    return CHROME_USER_DATA


def chrome_options(username=None):
    options = uc.ChromeOptions()
    user_data = chrome_user_data_dir(username)
    if user_data:
        os.makedirs(user_data, exist_ok=True)
        options.add_argument(f"--user-data-dir={user_data}")
        options.add_argument(f"--profile-directory={CHROME_PROFILE}")
    if PAGE_LOAD_STRATEGY in ("normal", "eager", "none"):
        options.page_load_strategy = PAGE_LOAD_STRATEGY
    return options


def detect_chrome_version():
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if not os.path.exists(path):
            continue
        try:
            output = subprocess.check_output(
                [path, "--version"],
                text=True,
                stderr=subprocess.STDOUT,
            )
            match = re.search(r"(\d+)\.", output)
            if match:
                return int(match.group(1))
        except (OSError, subprocess.SubprocessError):
            continue
    return None


_chrome_version_cache = None


def chrome_version_main():
    global _chrome_version_cache
    version = os.environ.get("BSCSCAN_CHROME_VERSION", "").strip()
    if version:
        return int(version)
    if _chrome_version_cache is None:
        _chrome_version_cache = detect_chrome_version() or 0
    return _chrome_version_cache or None


def cleanup_stale_chrome_browsers(user_data_dir=None):
    """Kill orphaned Chrome processes tied to our user-data profile."""
    profile_dir = (user_data_dir or chrome_user_data_dir()).strip()
    if not profile_dir:
        return 0

    try:
        output = subprocess.check_output(
            ["pgrep", "-f", profile_dir],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return 0

    killed = 0
    current_pid = os.getpid()
    for line in output.strip().splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        if pid == current_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue

    if killed:
        time.sleep(1)
        warn(f"Cleaned up {killed} stale Chrome process(es)")

    return killed


def cleanup_stale_chromedrivers(exclude_pid=None):
    """Kill orphaned chromedriver processes left after crashes."""
    try:
        output = subprocess.check_output(
            ["pgrep", "-x", "chromedriver"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return 0

    killed = 0
    for line in output.strip().splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        if exclude_pid and pid == exclude_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue

    if killed:
        time.sleep(0.5)
        warn(f"Cleaned up {killed} stale chromedriver process(es)")

    return killed


def terminate_driver(driver):
    """Quit Selenium and ensure the chromedriver service process is gone."""
    service_pid = None
    if driver is not None:
        try:
            service = getattr(driver, "service", None)
            if service and getattr(service, "process", None):
                service_pid = service.process.pid
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
        if service_pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(service_pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(0.2)

    cleanup_stale_chrome_browsers(chrome_user_data_dir())
    cleanup_stale_chromedrivers(exclude_pid=service_pid)


def capture_session_cookies(driver):
    """Read cookies from My Account so auth markers are included when present."""
    try:
        current = cookie_string(driver)
        if (
            "bscscan.com" in (driver.current_url or "")
            and any(marker in current for marker in AUTH_COOKIE_MARKERS)
        ):
            # The trip to /myaccount exists to pick up bscscan_userid/username,
            # which get_cookies() omits on some pages. If they're already here, the
            # extra page load (and 2.5s settle) buys nothing — and it used to
            # navigate away from the NFT page the caller had just opened.
            return current
    except WebDriverException:
        pass

    try:
        driver.get(MYACCOUNT_URL)
        time.sleep(STEP_DELAY_SECONDS)
        dismiss_cookie_banner(driver)
    except WebDriverException:
        pass
    return cookie_string(driver)


def start_chrome(options, version_main, use_cached_driver):
    """Start undetected Chrome, bounding any chromedriver download it performs."""
    kwargs = {"options": options, "user_multi_procs": use_cached_driver}
    if version_main:
        kwargs["version_main"] = version_main
    # The patcher's urlopen/urlretrieve calls have no timeout of their own; the
    # socket default is the only way to stop them blocking forever.
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(DRIVER_DOWNLOAD_TIMEOUT)
    try:
        return uc.Chrome(**kwargs)
    finally:
        socket.setdefaulttimeout(previous_timeout)


def cached_driver_unusable(error):
    """True when the cached chromedriver cannot serve this Chrome and must be re-downloaded."""
    if isinstance(error, ValueError):
        return True  # empty cache dir: patcher does max() over no files
    text = str(error).lower()
    return (
        "only supports chrome version" in text
        or "session not created" in text
        or "no such file or directory" in text
        or "not a valid" in text
    )


def create_driver(username=None):
    if username:
        set_active_chrome_user(username)
    user_data = chrome_user_data_dir(username)
    cleanup_stale_chrome_browsers(user_data)
    cleanup_stale_chromedrivers()
    version_main = chrome_version_main()
    last_error = None
    use_cached_driver = True

    if user_data:
        info(f"Chrome profile  {user_data}  ({CHROME_PROFILE})")
    if version_main:
        info(f"Chrome version_main  {version_main}")

    attempt = 1
    while attempt <= DRIVER_RETRIES:
        options = chrome_options(username)
        started = time.monotonic()
        try:
            driver = start_chrome(options, version_main, use_cached_driver)
            apply_command_timeout(driver)
            driver.get("about:blank")
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.set_script_timeout(30)
            info(f"Chrome started  ({time.monotonic() - started:.1f}s, {'cached' if use_cached_driver else 'downloaded'} chromedriver)")
            return driver
        except (TimeoutError, OSError, RuntimeError, ValueError, WebDriverException) as error:
            last_error = error
            if use_cached_driver and cached_driver_unusable(error):
                # Doesn't consume a retry: this is the expected path after a
                # Chrome update or on a fresh machine.
                warn(f"Cached chromedriver unusable ({str(error).strip().splitlines()[0][:90]}) — downloading a fresh one")
                use_cached_driver = False
                continue
            if attempt < DRIVER_RETRIES:
                print(f"Chrome driver init failed (attempt {attempt}/{DRIVER_RETRIES}): {error}")
                print(f"Retrying in {DRIVER_RETRY_DELAY}s...")
                time.sleep(DRIVER_RETRY_DELAY)
            attempt += 1

    raise RuntimeError(f"Could not start Chrome after {DRIVER_RETRIES} attempts: {last_error}")


def apply_command_timeout(driver, timeout=COMMAND_TIMEOUT):
    """Cap how long any single chromedriver command may block.

    Selenium's ClientConfig.timeout defaults to None, which urllib3 reads as "no
    timeout" — a hung renderer then stalls the run indefinitely with no output.
    """
    if timeout <= 0 or driver is None:
        return
    try:
        driver.command_executor._client_config.timeout = timeout
    except Exception:
        pass
    try:
        from selenium.webdriver.remote.remote_connection import RemoteConnection

        RemoteConnection._timeout = timeout
    except Exception:
        pass


def driver_is_alive(driver):
    try:
        _ = driver.current_url
        return True
    except (NoSuchWindowException, WebDriverException):
        return False


def quit_driver(driver):
    if driver is None:
        return
    terminate_driver(driver)


def logged_in_username(driver):
    try:
        for cookie in driver.get_cookies():
            if cookie.get("name") == "bscscan_username":
                value = (cookie.get("value") or "").strip()
                if value:
                    return value
    except Exception:
        pass

    try:
        if on_myaccount_page(driver):
            value = field_value(driver, "#ContentPlaceHolder1_txtUserName")
            if value:
                return value
    except Exception:
        pass
    return None


def username_matches(driver, username):
    """Confirm the current session belongs to `username`.

    BscScan frequently omits the bscscan_username cookie, and get_cookies() drops it
    on some pages (see accounts.pool.cookie_has_auth). Treating "unknown" as a
    mismatch threw away working sessions and forced a full Turnstile login on every
    browser restart. Chrome profiles are per-account, so fall back to the page text
    and then to trusting the profile. Set BSCSCAN_STRICT_USERNAME=1 for the old
    fail-closed behaviour.
    """
    logged_as = logged_in_username(driver)
    if logged_as:
        if logged_as.lower() == username.lower():
            return True
        warn(f"Logged in as {logged_as}, expected {username}")
        return False

    try:
        page = (driver.page_source or "").lower()
    except Exception:
        page = ""
    if is_cloudflare_html(page):
        warn(f"Cloudflare challenge on account page — cannot confirm session for {username}")
        return False
    if re.search(r"(?<![a-z0-9_])" + re.escape(username.lower()) + r"(?![a-z0-9_])", page):
        return True

    if STRICT_USERNAME_CHECK:
        warn(f"Could not verify logged-in username (expected {username})")
        return False

    warn(f"Username not shown on page — accepting signed-in session for {username}")
    return True


def try_recover_chrome_profile(driver, username, token_id=None):
    """Reuse a BscScan session already stored in the Chrome user-data profile."""
    if not driver_is_alive(driver):
        return False

    info(f"Checking Chrome profile session for {username}")
    try:
        driver.get(MYACCOUNT_URL)
        time.sleep(STEP_DELAY_SECONDS)
        dismiss_cookie_banner(driver)
        if not on_myaccount_page(driver) and not is_logged_in(driver):
            return False

        if not username_matches(driver, username):
            return False

        ok(f"Session recovered from Chrome profile  {username}")
        if token_id:
            visit_nft_page(driver, token_id)
        return True
    except (WebDriverException, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            warn(f"Chrome profile session unusable for {username} — {error}")
        return False


def reset_browser_session(driver):
    # The first load only exists to give delete_all_cookies a bscscan.com origin;
    # nothing reads the page, so don't wait for it to settle.
    driver.get(LOGIN_URL)
    driver.delete_all_cookies()
    driver.get(LOGIN_URL)
    time.sleep(STEP_DELAY_SECONDS)
    dismiss_cookie_banner(driver)


def open_login_page(driver, wait):
    if "/login" not in driver.current_url.lower():
        driver.get(LOGIN_URL)
        time.sleep(STEP_DELAY_SECONDS)
        dismiss_cookie_banner(driver)
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#ContentPlaceHolder1_txtUserName")))
    info(f"Login page open  {driver.current_url}")


def field_value(driver, selector):
    try:
        element = driver.find_element(By.CSS_SELECTOR, selector)
        return (element.get_attribute("value") or "").strip()
    except Exception:
        return ""


def set_field_value(driver, element, value):
    driver.execute_script(
        """
        arguments[0].scrollIntoView({block: 'center'});
        arguments[0].focus();
        arguments[0].value = arguments[1];
        arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
        """,
        element,
        value,
    )


def fill_credentials(driver, wait, username, password):
    open_login_page(driver, wait)

    username_input = wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "#ContentPlaceHolder1_txtUserName"))
    )
    set_field_value(driver, username_input, username)
    time.sleep(0.3)

    password_input = wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "#ContentPlaceHolder1_txtPassword"))
    )
    set_field_value(driver, password_input, password)
    time.sleep(0.3)

    if field_value(driver, "#ContentPlaceHolder1_txtUserName") != username:
        slow_type(driver, username_input, username)
    if field_value(driver, "#ContentPlaceHolder1_txtPassword") != password:
        slow_type(driver, password_input, password)

    username_ok = field_value(driver, "#ContentPlaceHolder1_txtUserName") == username
    password_ok = bool(field_value(driver, "#ContentPlaceHolder1_txtPassword"))
    if not username_ok or not password_ok:
        raise RuntimeError(
            f"Could not fill login form (username_ok={username_ok}, password_ok={password_ok}). "
            f"URL: {driver.current_url}"
        )
    ok(f"Filled username/password for {username}")


def ensure_credentials_filled(driver, wait, username, password):
    username_ok = field_value(driver, "#ContentPlaceHolder1_txtUserName") == username
    password_ok = bool(field_value(driver, "#ContentPlaceHolder1_txtPassword"))
    if username_ok and password_ok:
        return
    warn("Login form was cleared — re-filling username/password")
    fill_credentials(driver, wait, username, password)


def banner_text_present(driver):
    """Cheap gate for dismiss_cookie_banner's expensive XPath fallback."""
    try:
        return bool(driver.execute_script(
            "return ((document.body && document.body.innerText) || '').includes('Got it');"
        ))
    except Exception:
        return True  # can't tell — fall through to the scan


def dismiss_cookie_banner(driver):
    # //*[contains(normalize-space(), 'Got it')] makes chromedriver normalize the
    # text of every node on the page. This runs after nearly every navigation, so
    # skip it entirely when the banner text isn't on the page at all.
    if not banner_text_present(driver):
        return
    for xpath in (
        "//button[contains(normalize-space(), 'Got it')]",
        "//*[not(self::script)][contains(normalize-space(text()), 'Got it')]",
    ):
        try:
            button = driver.find_element(By.XPATH, xpath)
            if button.is_displayed():
                driver.execute_script("arguments[0].click();", button)
                time.sleep(1)
                return
        except Exception:
            continue


def on_myaccount_page(driver):
    url = driver.current_url.lower()
    if "/login" in url:
        return False
    if "myaccount" in url:
        return True  # URL alone settles it — skip the page_source round trip
    page = driver.page_source.lower()
    return (
        "account overview" in page
        or "personal info" in page
        or ("sign out" in page and "contentplaceholder1_btnlogin" not in page)
    )


def is_logged_in(driver):
    try:
        # get_cookies() is far cheaper than serialising page_source, and these loops
        # run at 1-2Hz for up to 25s.
        cookie_names = {cookie.get("name") for cookie in driver.get_cookies()}
        if "bscscan_userid" in cookie_names or "bscscan_username" in cookie_names:
            return True
        if on_myaccount_page(driver):
            return True
    except Exception:
        return False
    return False


def confirm_logged_in(driver):
    driver.get(MYACCOUNT_URL)
    time.sleep(STEP_DELAY_SECONDS)
    dismiss_cookie_banner(driver)
    return on_myaccount_page(driver)


def confirm_logged_in_as(driver, username):
    if not confirm_logged_in(driver):
        return False
    return username_matches(driver, username)


def poll_until_logged_in(driver, username, timeout=30):
    info(f"Waiting for login  {username}  (up to {int(timeout)}s, no action needed if browser already signed in)")
    end = time.time() + timeout
    while time.time() < end:
        # Success check first: it is now URL/cookie-only in the common case, while
        # is_browser_error_page always pays for page_source.
        if on_myaccount_page(driver) or is_logged_in(driver):
            ok(f"Logged in as {username}")
            return True
        if is_browser_error_page(driver):
            warn("Browser shows connection error — trying My Account directly")
            if confirm_logged_in(driver):
                ok(f"Logged in as {username}")
                return True
            break
        time.sleep(1)
    if confirm_logged_in(driver):
        ok(f"Logged in as {username}")
        return True
    return False


def page_has_captcha_error(driver, page=None):
    if page is None:
        page = driver.page_source.lower()
    return "invalid captcha response" in page or "captcha verification failed" in page


def is_browser_error_page(driver, page=None):
    try:
        url = (driver.current_url or "").lower()
        if page is None:
            page = driver.page_source.lower()
    except Exception:
        return False
    if url.startswith("chrome-error://") or "chromewebdata" in url:
        return True
    return any(
        phrase in page
        for phrase in (
            "site can't be reached",
            "site can’t be reached",
            "cannot connect",
            "could not connect",
            "err_connection",
            "err_name_not_resolved",
            "err_timed_out",
            "err_internet_disconnected",
            "had an error",
        )
    )


def reload_login_page(driver, wait, username, password):
    warn("Reloading login page after connection error")
    driver.get(LOGIN_URL)
    time.sleep(STEP_DELAY_SECONDS)
    dismiss_cookie_banner(driver)
    fill_credentials(driver, wait, username, password)


def stdin_is_interactive():
    if MANUAL_LOGIN:
        return True
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def turnstile_token(driver):
    return driver.execute_script(
        """
        const field = document.querySelector('[name="cf-turnstile-response"]');
        return field && field.value ? field.value : '';
        """
    )


def slow_type(driver, element, value):
    driver.execute_script(
        """
        arguments[0].scrollIntoView({block: 'center'});
        arguments[0].focus();
        arguments[0].value = '';
        arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
        """,
        element,
    )
    time.sleep(0.3)
    element.click()
    for char in value:
        element.send_keys(char)
        time.sleep(TYPE_DELAY_SECONDS)
    if element.get_attribute("value") != value:
        set_field_value(driver, element, value)


def turnstile_state(driver):
    """Return (token, widget_present) for the Turnstile challenge on this page."""
    try:
        state = driver.execute_script(
            """
            const field = document.querySelector('[name="cf-turnstile-response"]');
            const widget = document.querySelector(
                '.cf-turnstile, #cf-turnstile, [data-sitekey], '
                + 'iframe[src*="challenges.cloudflare.com"]'
            );
            return {token: (field && field.value) || '', present: !!(field || widget)};
            """
        ) or {}
    except WebDriverException:
        return "", True
    return state.get("token") or "", bool(state.get("present"))


def wait_for_turnstile(driver, timeout=CAPTCHA_WAIT_SECONDS):
    info("Waiting for Turnstile (complete captcha in browser if shown)...")
    started = time.time()
    end = started + timeout
    while time.time() < end:
        token, present = turnstile_state(driver)
        if token:
            time.sleep(POST_TURNSTILE_DELAY)
            ok(f"Turnstile completed ({time.time() - started:.1f}s)")
            return True
        # Neither the response field nor the widget is in the DOM: the challenge
        # never rendered, and polling for the remaining timeout cannot make one
        # appear. Bail early so the caller can reload instead of stalling.
        if not present and time.time() - started >= TURNSTILE_ABSENT_GRACE:
            warn(
                f"No Turnstile widget rendered after {TURNSTILE_ABSENT_GRACE:.0f}s "
                f"(page: {driver.current_url}) — reloading instead of waiting {int(timeout)}s"
            )
            return False
        time.sleep(0.5)
    warn(f"Turnstile not detected within {int(timeout)}s")
    return False


def submit_login(driver, wait):
    submit = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "#ContentPlaceHolder1_btnLogin"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit)
    time.sleep(0.3)
    driver.execute_script("arguments[0].click();", submit)


def wait_for_login_result(driver, username, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if on_myaccount_page(driver) or is_logged_in(driver):
            ok(f"Logged in as {username}")
            return True
        try:
            page = driver.page_source.lower()  # one read covers both checks below
        except Exception:
            page = ""
        if is_browser_error_page(driver, page=page):
            return False
        if page_has_captcha_error(driver, page=page):
            return False
        time.sleep(0.5)
    return on_myaccount_page(driver) or is_logged_in(driver)


def submit_login_and_wait(driver, wait, username, password):
    if not turnstile_token(driver):
        warn("Turnstile token missing before LOGIN — waiting...")
        if not wait_for_turnstile(driver, timeout=15):
            return False

    info("Clicking LOGIN...")
    submit_login(driver, wait)
    time.sleep(LOGIN_SUBMIT_WAIT)

    if is_browser_error_page(driver):
        return False

    if page_has_captcha_error(driver):
        warn("Captcha rejected")
        return False

    if wait_for_login_result(driver, username):
        return True

    return confirm_logged_in(driver)


def wait_for_manual_login(driver, username):
    if poll_until_logged_in(driver, username, timeout=5):
        return True
    if not stdin_is_interactive():
        warn(f"Login not detected for {username} (unattended — will retry)")
        return False
    warn(
        f"Login not detected for {username}."
        "\n    Click LOGIN in the browser if needed, wait for My Account, then press ENTER"
    )
    try:
        input()
    except EOFError:
        warn(f"Login not detected for {username} (no stdin — will retry)")
        return False
    if poll_until_logged_in(driver, username, timeout=15):
        return True
    return confirm_logged_in(driver)


def complete_login_after_turnstile(driver, wait, username, password):
    # wait_for_turnstile already slept POST_TURNSTILE_DELAY after the token landed.
    ensure_credentials_filled(driver, wait, username, password)

    for attempt in range(1, 3):
        if submit_login_and_wait(driver, wait, username, password):
            return True

        if page_has_captcha_error(driver):
            if stdin_is_interactive():
                warn("Click LOGIN manually in the browser if needed")
                ensure_credentials_filled(driver, wait, username, password)
                if wait_for_manual_login(driver, username):
                    return True
            return False

        if attempt < 2:
            reload_login_page(driver, wait, username, password)
            if not wait_for_turnstile(driver, timeout=CAPTCHA_WAIT_SECONDS):
                continue
            ensure_credentials_filled(driver, wait, username, password)
            continue

    if confirm_logged_in(driver):
        ok(f"Logged in as {username}")
        return True

    return wait_for_manual_login(driver, username)


def login(driver, username, password):
    wait = WebDriverWait(driver, 45)
    last_error = None

    for attempt in range(1, LOGIN_RETRIES + 1):
        if attempt > 1:
            driver.get(LOGIN_URL)
            time.sleep(STEP_DELAY_SECONDS)
            dismiss_cookie_banner(driver)

        info(f"Login attempt {attempt}/{LOGIN_RETRIES}  {username}")
        fill_credentials(driver, wait, username, password)
        if not wait_for_turnstile(driver, timeout=CAPTCHA_WAIT_SECONDS):
            last_error = RuntimeError(f"Turnstile not ready for {username}")
            continue

        if complete_login_after_turnstile(driver, wait, username, password):
            time.sleep(STEP_DELAY_SECONDS)
            return

        last_error = RuntimeError(f"Login failed for {username}")

    raise last_error or RuntimeError(f"Login failed for {username}")


def cookie_string(driver):
    cookies = sorted(driver.get_cookies(), key=lambda item: item.get("name", ""))
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name"))


def nft_page_url(token_id):
    return f"https://bscscan.com/nft/{CONTRACT}/{token_id}"


def nft_page_ready(driver, page=None):
    html = driver.page_source if page is None else page
    if is_cloudflare_html(html):
        return False
    return 'id="collapseProperties"' in html or "__VIEWSTATE" in html


def selenium_login(driver, username, password):
    """Step 1: Log in via Selenium and confirm session before visiting NFT page."""
    info(f"Step 1/3  Selenium login  {username}")
    started = time.monotonic()
    login(driver, username, password)
    if not confirm_logged_in(driver):
        raise RuntimeError(
            f"Login failed for {username} — complete login in browser, then retry"
        )
    ok(f"Login successful  {username}  ({time.monotonic() - started:.1f}s)")


def visit_nft_page(driver, token_id):
    """Step 2: Open the first token's NFT page after login."""
    url = nft_page_url(token_id)
    short_id = str(token_id)
    if len(short_id) > 12:
        short_id = "…" + short_id[-10:]

    info(f"Step 2/3  Open first token NFT page  {short_id}")
    info(f"           {url}")

    driver.get(url)
    time.sleep(STEP_DELAY_SECONDS)
    dismiss_cookie_banner(driver)

    if not nft_page_ready(driver):
        raise RuntimeError(
            f"NFT page did not load (Cloudflare or not logged in).\n  URL: {url}"
        )
    ok(f"NFT page loaded  {short_id}")


def capture_session(driver, username):
    """Step 3: Read cookie + userAgent from the current (NFT) page."""
    info(f"Step 3/3  Capture cookie + userAgent  {username}")

    cookies = capture_session_cookies(driver)
    if not cookies:
        raise RuntimeError(f"No cookies captured for {username}")

    user_agent = driver.execute_script("return navigator.userAgent") or DEFAULT_USER_AGENT
    cookie_count = len([part for part in cookies.split(";") if part.strip()])
    has_clearance = "cf_clearance=" in cookies
    has_session = "ASP.NET_SessionId=" in cookies

    ok(
        f"Captured  {cookie_count} cookies"
        f"  cf_clearance={has_clearance}  session={has_session}"
    )
    return cookies, user_agent


def capture_account(pool, username, password, token_id, driver=None):
    owns_driver = driver is None
    if owns_driver:
        driver = create_driver(username=username)

    try:
        for attempt in range(1, DRIVER_RETRIES + 1):
            try:
                if not driver_is_alive(driver):
                    if not owns_driver:
                        raise RuntimeError("Shared browser window closed — cannot recover")
                    warn(f"Browser window closed — restarting Chrome ({attempt}/{DRIVER_RETRIES})")
                    quit_driver(driver)
                    driver = create_driver(username=username)

                reset_browser_session(driver)
                selenium_login(driver, username, password)
                visit_nft_page(driver, token_id)
                cookies, user_agent = capture_session(driver, username)
                pool.save_session(username, cookies, user_agent)
                ok(f"Saved to account state  {username}")
                return True
            except NoSuchWindowException:
                if not owns_driver or attempt >= DRIVER_RETRIES:
                    raise
                warn(f"Browser window closed — restarting Chrome ({attempt}/{DRIVER_RETRIES})")
                quit_driver(driver)
                driver = create_driver(username=username)

        raise RuntimeError(f"Could not capture session for {username} — browser kept closing")
    finally:
        if owns_driver:
            quit_driver(driver)


def parse_args():
    parser = argparse.ArgumentParser(description="Log into BscScan and save cookies for account rotation.")
    parser.add_argument("--all", action="store_true", help="Log in every account in accounts/accounts.json")
    parser.add_argument("--account", help="Log in one account by username")
    parser.add_argument(
        "--count",
        type=int,
        help=f"Log in the first N accounts (budget ~{TOKENS_PER_COOKIE} tokens/cookie)",
    )
    parser.add_argument(
        "--for-crawl",
        action="store_true",
        help=f"Log in ceil(tokens/{TOKENS_PER_COOKIE}) accounts from tokens.csv",
    )
    parser.add_argument(
        "--for-refresh",
        action="store_true",
        help=f"Log in ceil((outOfSync + crawl errors)/{REFRESH_TOKENS_PER_COOKIE}) accounts from compare report",
    )
    parser.add_argument("--token-id", default=None)
    args = parser.parse_args()
    if args.token_id is None:
        args.token_id = os.environ.get("TOKEN_ID") or first_token_id()
    return args


def resolve_login_targets(pool, args):
    if args.all:
        print("Clearing existing cookies...")
        pool.clear_sessions()
        return pool.usernames()

    if args.for_crawl:
        token_count = count_token_ids()
        needed = cookies_needed(token_count)
        print(f"Crawl needs {needed} cookie(s) for {token_count} token(s) ({TOKENS_PER_COOKIE}/cookie)")
        return pool.usernames()[:needed]

    if args.for_refresh:
        token_count = refresh_target_counts()["total"]
        needed = refresh_cookies_needed(token_count)
        print(f"Refresh needs {needed} cookie(s) for {token_count} token(s) ({REFRESH_TOKENS_PER_COOKIE}/cookie)")
        return pool.usernames()[:needed]

    if args.count:
        return pool.usernames()[: args.count]

    pin = os.environ.get("BSCSCAN_ACCOUNT") or args.account
    if pin:
        pool.get_credentials(pin)
        return [pin]

    missing = pool.missing_cookie_usernames()
    return missing[:1] if missing else [pool.usernames()[0]]


def main():
    args = parse_args()
    pool = AccountPool()
    targets = resolve_login_targets(pool, args)

    if not targets:
        raise SystemExit("No accounts configured.")

    print(f"Logging in {len(targets)} account(s)...")
    for index, username in enumerate(targets):
        creds = pool.get_credentials(username)
        print(f"[{index + 1}/{len(targets)}] {username}")
        capture_account(pool, username, creds["password"], args.token_id, driver=None)
        if index + 1 < len(targets):
            time.sleep(ACCOUNT_GAP_SECONDS)

    missing = pool.missing_cookie_usernames()
    if missing:
        print(f"Accounts still missing cookies: {', '.join(missing)}")
    print("Next: python3 refresh-metadata/refresh.py  or  python3 crawl/compare.py")


if __name__ == "__main__":
    main()
