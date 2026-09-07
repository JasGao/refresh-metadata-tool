import os

from accounts.pool import AccountPool, USAGE_REFRESH
from lib.tokenids import REFRESH_TOKENS_PER_COOKIE


def configure_pool_allowed(pool):
    """Limit account rotation to BSCSCAN_ALLOWED_ACCOUNTS or BSCSCAN_ACCOUNT."""
    allowed = os.environ.get("BSCSCAN_ALLOWED_ACCOUNTS", "").strip()
    if allowed:
        pool.set_allowed([name.strip() for name in allowed.split(",") if name.strip()])
        return
    pin = os.environ.get("BSCSCAN_ACCOUNT", "").strip()
    if pin:
        pool.set_allowed([pin])


def account_env_for_refresh_tokens(token_count, limit=REFRESH_TOKENS_PER_COOKIE):
    """Build env dict scoped to all accounts with available refresh quota.

    The `limit` should match the per-account refresh quota used by
    `refresh-metadata/refresh.py` so quota eligibility is computed consistently.
    """
    if token_count <= 0:
        return {"BSCSCAN_ALLOWED_ACCOUNTS": ""}

    pool = AccountPool()

    # Don't rely on AccountPool.available_usernames() here because it calls
    # get_refresh_usage() with the module default (100). We want the same
    # `limit` as refresh.py for accurate eligibility.
    candidates = []
    for username in pool.pool_usernames():
        if pool.is_exhausted(username, USAGE_REFRESH):
            continue
        usage = pool.get_refresh_usage(username, limit=limit)
        if int(usage.get("remaining", 0)) <= 0:
            continue
        candidates.append(username)

    if not candidates:
        raise SystemExit("No accounts with available refresh quota")

    # Keep pool rotation semantics: if currentIndex is in candidates, start there.
    current = pool.usernames()[pool.state.get("currentIndex", 0) % len(pool.usernames())]
    if current in candidates:
        idx = candidates.index(current)
        candidates = candidates[idx:] + candidates[:idx]

    pool.set_allowed(candidates)
    pool.reset_to_first_allowed()
    return {"BSCSCAN_ALLOWED_ACCOUNTS": ",".join(candidates)}
