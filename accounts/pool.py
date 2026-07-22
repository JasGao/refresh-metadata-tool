import json
import os
from datetime import datetime, timedelta, timezone

from lib.env import load_dotenv
from lib.paths import ACCOUNTS_FILE, ACCOUNT_STATE_FILE
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

AUTH_COOKIE_MARKERS = ("bscscan_userid=", "bscscan_username=")

USAGE_CRAWL = "crawl"
USAGE_REFRESH = "refresh"  # full flow: account must pass GET + POST checks
USAGE_REFRESH_GET = "refresh_get"
USAGE_REFRESH_POST = "refresh_post"
EXHAUSTED_FIELDS = {
    USAGE_CRAWL: "crawlExhaustedUntil",
    USAGE_REFRESH: "refreshExhaustedUntil",
    USAGE_REFRESH_GET: "refreshGetExhaustedUntil",
    USAGE_REFRESH_POST: "refreshPostExhaustedUntil",
}
REFRESH_EXHAUSTED_LEGACY = "refreshExhaustedUntil"
REFRESH_USAGE_FIELD = "refreshUsage"


def cookie_has_auth(cookie):
    if not cookie:
        return False
    if any(marker in cookie for marker in AUTH_COOKIE_MARKERS):
        return True
    # BscScan login is often carried by ASP.NET session + Cloudflare clearance only;
    # Selenium's get_cookies() may omit bscscan_userid on some pages.
    return "ASP.NET_SessionId=" in cookie and "cf_clearance=" in cookie and len(cookie) > 200


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_day():
    return _now().strftime("%Y-%m-%d")


class AccountPool:
    def __init__(self, accounts_path=None, state_path=None):
        self.accounts_path = accounts_path or self._resolve_accounts_path()
        self.state_path = state_path or ACCOUNT_STATE_FILE
        self.accounts = self._load_accounts()
        self.state = self._load_state()
        self.allowed_usernames = None

    def set_allowed(self, usernames):
        if not usernames:
            self.allowed_usernames = None
            return
        known = set(self.usernames())
        self.allowed_usernames = [name for name in usernames if name in known]

    def pool_usernames(self):
        if self.allowed_usernames:
            return list(self.allowed_usernames)
        return self.usernames()

    def reset_to_first_allowed(self):
        names = self.pool_usernames()
        if not names:
            return
        self.state["currentIndex"] = self.usernames().index(names[0])
        self.save()

    def _resolve_accounts_path(self):
        if os.path.exists(ACCOUNTS_FILE):
            return ACCOUNTS_FILE
        raise FileNotFoundError("No account file found. Create accounts/accounts.json")

    def _load_accounts(self):
        load_dotenv()
        password = os.environ.get("BSCSCAN_PASSWORD", "").strip()
        if not password:
            raise ValueError(
                "Set BSCSCAN_PASSWORD in .env (or the environment). "
                "All accounts share one password."
            )
        with open(self.accounts_path, "r") as file:
            data = json.load(file)
        if not isinstance(data, list) or not data:
            raise ValueError(f"{self.accounts_path} must be a non-empty JSON array")
        accounts = []
        for account in data:
            if isinstance(account, str):
                username = account.strip()
            else:
                username = (account.get("username") or "").strip()
            if not username:
                raise ValueError("Each account needs a username")
            accounts.append({"username": username, "password": password})
        return accounts

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return {"currentIndex": 0, "sessions": {}}
        with open(self.state_path, "r") as file:
            return json.load(file)

    def save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)

        # Prevent concurrent writes from corrupting shared state.json.
        # A simple file lock is enough since refresh/login/crawl are single-process tools.
        lock_path = os.path.join(os.path.dirname(self.state_path), "state.lock")
        if fcntl is None:
            # Best-effort fallback for platforms without fcntl.
            with open(self.state_path, "w") as file:
                json.dump(self.state, file, indent=2)
            os.chmod(self.state_path, 0o600)
            return

        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            tmp_path = f"{self.state_path}.tmp"
            with open(tmp_path, "w") as file:
                json.dump(self.state, file, indent=2)
            os.replace(tmp_path, self.state_path)
            os.chmod(self.state_path, 0o600)

    def clear_sessions(self):
        self.state = {"currentIndex": 0, "sessions": {}}
        self.save()

    def usernames(self):
        return [account["username"] for account in self.accounts]

    def get_credentials(self, username):
        for account in self.accounts:
            if account["username"] == username:
                return account
        raise KeyError(f"Unknown account: {username}")

    def session(self, username):
        return self.state.setdefault("sessions", {}).setdefault(username, {})

    def _exhausted_until(self, username, usage):
        entry = self.session(username)
        field = EXHAUSTED_FIELDS.get(usage)
        if field:
            until = _parse_iso(entry.get(field))
            if until is not None:
                return until
        if usage in (USAGE_REFRESH_GET, USAGE_REFRESH_POST):
            until = _parse_iso(entry.get(REFRESH_EXHAUSTED_LEGACY))
            if until is not None:
                return until
        return _parse_iso(entry.get("exhaustedUntil"))

    def _is_available(self, username, usage):
        if usage == USAGE_REFRESH:
            return (
                not self.is_exhausted(username, USAGE_REFRESH)
                and not self.is_exhausted(username, USAGE_REFRESH_GET)
                and not self.is_exhausted(username, USAGE_REFRESH_POST)
                and self.get_refresh_usage(username)["remaining"] > 0
            )
        return not self.is_exhausted(username, usage)

    def is_exhausted(self, username, usage):
        exhausted_until = self._exhausted_until(username, usage)
        return exhausted_until is not None and exhausted_until > _now()

    def mark_exhausted(self, username, usage):
        tomorrow = _now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        entry = self.session(username)
        entry[EXHAUSTED_FIELDS[usage]] = _iso(tomorrow)
        self.save()
        print(f"Marked {username} {usage}-exhausted until {entry[EXHAUSTED_FIELDS[usage]]}")

    def init_refresh_usage(self, username, limit=REFRESH_TOKENS_PER_COOKIE):
        """Create or roll over daily refresh quota for the account."""
        entry = self.session(username)
        today = _utc_day()
        usage = entry.get(REFRESH_USAGE_FIELD) or {}
        if usage.get("day") != today or "remaining" not in usage:
            entry[REFRESH_USAGE_FIELD] = {
                "day": today,
                "startedAt": None,
                "used": 0,
                "limit": limit,
                "remaining": limit,
            }
            entry.pop("cookie", None)
            entry.pop("userAgent", None)
            self.save()
        return entry[REFRESH_USAGE_FIELD]

    def get_refresh_usage(self, username, limit=REFRESH_TOKENS_PER_COOKIE):
        usage = self.init_refresh_usage(username, limit=limit)
        return {
            "day": usage["day"],
            "startedAt": usage.get("startedAt"),
            "used": int(usage.get("used", 0)),
            "limit": int(usage.get("limit", limit)),
            "remaining": max(0, int(usage.get("remaining", limit))),
        }

    def mark_refresh_usage_started(self, username, limit=REFRESH_TOKENS_PER_COOKIE):
        usage = self.init_refresh_usage(username, limit=limit)
        if not usage.get("startedAt"):
            last_login = _parse_iso(self.session(username).get("lastLogin"))
            if last_login and last_login.strftime("%Y-%m-%d") == _utc_day():
                usage["startedAt"] = _iso(last_login)
            else:
                usage["startedAt"] = _iso(_now())
            self.save()
        return usage

    def refresh_usage_started_today(self, username):
        usage = self.session(username).get(REFRESH_USAGE_FIELD) or {}
        started = _parse_iso(usage.get("startedAt"))
        if not started:
            return False
        return started.strftime("%Y-%m-%d") == _utc_day()

    def can_reuse_refresh_session(self, username, limit=REFRESH_TOKENS_PER_COOKIE):
        cookie = self.get_cookie(username)
        if not cookie or not cookie_has_auth(cookie):
            return False
        if self.is_exhausted(username, USAGE_REFRESH):
            return False
        usage = self.get_refresh_usage(username, limit=limit)
        if usage["remaining"] <= 0:
            return False
        if self.refresh_usage_started_today(username):
            return True
        last_login = _parse_iso(self.session(username).get("lastLogin"))
        return bool(last_login and last_login.strftime("%Y-%m-%d") == _utc_day())

    def needs_crawl_login(self, username):
        cookie = self.get_cookie(username)
        if not cookie or not cookie_has_auth(cookie):
            return True
        last_login = _parse_iso(self.session(username).get("lastLogin"))
        return not last_login or last_login.strftime("%Y-%m-%d") != _utc_day()

    def note_refresh_used(self, username, count=1, limit=REFRESH_TOKENS_PER_COOKIE):
        usage = self.init_refresh_usage(username, limit=limit)
        used = min(usage["limit"], usage["used"] + count)
        usage["used"] = used
        usage["remaining"] = max(0, usage["limit"] - used)
        self.save()
        return usage

    def init_pool_refresh_usage(self, usernames=None, limit=REFRESH_TOKENS_PER_COOKIE):
        names = usernames or self.pool_usernames()
        return {username: self.init_refresh_usage(username, limit=limit) for username in names}

    def save_session(self, username, cookie, user_agent=None, clear_exhaustion=True):
        entry = self.session(username)
        entry["cookie"] = cookie
        entry["userAgent"] = user_agent or DEFAULT_USER_AGENT
        entry["lastLogin"] = _iso(_now())
        if clear_exhaustion:
            entry[EXHAUSTED_FIELDS[USAGE_CRAWL]] = None
            entry[EXHAUSTED_FIELDS[USAGE_REFRESH]] = None
            entry[EXHAUSTED_FIELDS[USAGE_REFRESH_GET]] = None
            entry[EXHAUSTED_FIELDS[USAGE_REFRESH_POST]] = None
            entry.pop(REFRESH_EXHAUSTED_LEGACY, None)
            entry.pop("exhaustedUntil", None)
        self.save()

    def get_cookie(self, username):
        return self.session(username).get("cookie", "")

    def get_user_agent(self, username):
        return self.session(username).get("userAgent") or DEFAULT_USER_AGENT

    def available_usernames(self, usage, pin=None):
        names = self.pool_usernames()
        if pin:
            return [pin] if pin in names and self._is_available(pin, usage) else []
        available = [name for name in names if self._is_available(name, usage)]
        if not available:
            return []
        all_names = self.usernames()
        current = all_names[self.state.get("currentIndex", 0) % len(all_names)]
        if current in available:
            idx = available.index(current)
            return available[idx:] + available[:idx]
        return available

    def get_active(self, usage, pin=None, require_cookie=True):
        available = self.available_usernames(usage, pin=pin)
        for username in available:
            cookie = self.get_cookie(username)
            if require_cookie and not cookie:
                continue
            self.state["currentIndex"] = self.usernames().index(username)
            self.save()
            creds = self.get_credentials(username)
            account = {
                "username": username,
                "password": creds["password"],
            }
            if cookie:
                account["cookie"] = cookie
                account["userAgent"] = self.get_user_agent(username)
            return account
        return None

    def rotate(self, usage, current_username=None, require_cookie=True):
        names = self.pool_usernames()
        if not names:
            return None
        if len(names) <= 1:
            return None

        if current_username and current_username in names:
            start_idx = (names.index(current_username) + 1) % len(names)
        else:
            start_idx = 0

        for offset in range(len(names)):
            next_username = names[(start_idx + offset) % len(names)]
            if current_username and next_username == current_username:
                continue
            if not self._is_available(next_username, usage):
                continue
            self.state["currentIndex"] = self.usernames().index(next_username)
            self.save()
            return self.get_active(usage, pin=next_username, require_cookie=require_cookie)
        return None

    def missing_cookie_usernames(self):
        return [name for name in self.usernames() if not self.get_cookie(name)]
