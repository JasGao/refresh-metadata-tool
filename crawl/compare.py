#!/usr/bin/env python3
"""
Compare BSCScan's cached NFT properties against live tokenURI metadata
for every token in tokens.csv.

Usage:
  python3 crawl/compare.py

Env:
  BNB_MAINNET_RPC_URL  RPC endpoint (falls back to public bsc-dataseed)
  BSCSCAN_ACCOUNT      pin one account username (optional)
  LIMIT                only process the first N tokens (quick test)
  RESET=1              wipe progress + report before starting
"""

import argparse
import html
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import accounts.login as account_login
from accounts.pool import AccountPool, DEFAULT_USER_AGENT, USAGE_CRAWL
from lib.detect import is_cloudflare_html, is_rate_limited_text
from lib.log_util import banner, fail, info, kv, ok, short_token, summary, warn
from lib.pool_config import configure_pool_allowed
from lib.paths import CRAWL_OUTPUT_DIR
from lib.reset_compare import PROGRESS_FILE, REPORT_FILE, reset_compare_files
from lib.tokenids import TOKEN_IDS_FILE, load_token_ids

CONTRACT = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
RPC_URL = os.environ.get("BNB_MAINNET_RPC_URL", "https://bsc-dataseed.binance.org")
# Parallel tokens per batch. Probed 2026-09-07: 50 concurrent fetches against
# BscScan, the RPC and renaiss.xyz all completed with no errors or throttling;
# latency roughly doubles between 20 and 50, so 100 buys little and raises the
# odds of a Cloudflare challenge. Override with CRAWL_CONCURRENCY=25 if the
# production Mac's network starts timing out at 50.
CONCURRENCY = int(os.environ.get("CRAWL_CONCURRENCY", "50"))
DELAY_SECONDS = float(os.environ.get("CRAWL_BATCH_DELAY", "1.5"))
HTTP_TIMEOUT = float(os.environ.get("CRAWL_HTTP_TIMEOUT", "30"))
# Transient network failures (timeouts, resets, SSL EOF) used to go straight
# into the report as crawl errors, and every one of those then cost a Selenium
# refresh even though the token was almost always in sync. Retry them first.
RETRIES = int(os.environ.get("CRAWL_RETRIES", "3"))
RETRY_DELAY_SECONDS = float(os.environ.get("CRAWL_RETRY_DELAY", "2"))
TRANSIENT_ERRORS = (socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException)
TRANSIENT_HTTP_CODES = (429, 500, 502, 503, 504)
# BscScan 503 means "rotate account", not "retry" — leave it to fetch_bscscan.
BSCSCAN_TRANSIENT_HTTP_CODES = (429, 500, 502, 504)

# After a BscScan 429 every worker pauses this long before its next request,
# instead of 50 threads retrying into the same throttle.
THROTTLE_PAUSE_SECONDS = float(os.environ.get("CRAWL_THROTTLE_PAUSE", "20"))

pool = AccountPool()
active_account = None
cookie = ""
user_agent = DEFAULT_USER_AGENT
# Bumped on every rotation. A worker that failed with an older generation must
# NOT rotate again: another worker already did, and it should simply retry with
# the new cookie. Without this, one Cloudflare event on a 50-token batch rotated
# through five accounts back to back (2026-09-07 batch 900) — and each account
# without a cookie means a Turnstile login inside the lock.
cookie_generation = 0
cloudflare_challenged = False
cookie_lock = threading.RLock()
throttle_until = 0.0


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decode_entities(text):
    def once(value):
        # `html.unescape` handles both named and numeric entities.
        # BscScan sometimes returns mixed-case entities like `&Amp;`,
        # so normalize those first before unescaping.
        normalized = re.sub(r"&([a-zA-Z]+);", lambda match: f"&{match.group(1).lower()};", value)
        return html.unescape(normalized)

    return once(once(text)).strip()


def norm(value):
    decoded = decode_entities(str(value or ""))
    return re.sub(r"[_\-'’`.\s]+", " ", decoded.lower()).strip()


def http_get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def describe_error(error):
    """Short, host-agnostic description for logs and the report."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error) or type(error).__name__


def is_transient_error(error, transient_codes=TRANSIENT_HTTP_CODES):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in transient_codes
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, TRANSIENT_ERRORS):
            return True
        text = str(reason).lower()
        return "timed out" in text or "eof occurred" in text or "connection reset" in text
    return isinstance(error, TRANSIENT_ERRORS)


def with_retries(stage, label, fn, transient_codes=TRANSIENT_HTTP_CODES):
    """Run fn(), retrying transient network errors RETRIES times with backoff.

    Non-transient errors (and the final transient failure) propagate unchanged
    so callers can still special-case e.g. BscScan HTTP 503.
    """
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except Exception as error:
            if attempt >= RETRIES or not is_transient_error(error, transient_codes):
                raise
            warn(f"{label}  {stage} retry {attempt}/{RETRIES - 1}: {describe_error(error)}", indent=4)
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def init_account():
    global active_account, cookie, user_agent
    configure_pool_allowed(pool)
    pool.reset_to_first_allowed()
    allowed = pool.pool_usernames()
    pin = os.environ.get("BSCSCAN_ACCOUNT")
    active_account = pool.get_active(USAGE_CRAWL, pin=pin)
    if not active_account:
        missing = [name for name in allowed if not pool.get_cookie(name)]
        hint = f"python3 accounts/login.py --account {allowed[0]}" if allowed else "python3 accounts/login.py --for-crawl"
        extra = f" (missing: {', '.join(missing)})" if missing else ""
        raise SystemExit(f"No account cookies found. Run: {hint}{extra}")
    cookie = active_account["cookie"]
    user_agent = active_account.get("userAgent") or DEFAULT_USER_AGENT
    kv("Account pool", ", ".join(allowed))
    kv("Active account", active_account["username"])


def respect_throttle():
    delay = throttle_until - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def note_throttled(seconds=THROTTLE_PAUSE_SECONDS):
    global throttle_until
    with cookie_lock:
        if throttle_until < time.monotonic():
            warn(f"BscScan throttling (HTTP 429) — pausing all workers {seconds:.0f}s", indent=4)
        throttle_until = max(throttle_until, time.monotonic() + seconds)


def rotate_cookie(reason, mark_exhausted=False, seen_generation=None):
    global active_account, cookie, user_agent, cloudflare_challenged, cookie_generation
    with cookie_lock:
        if seen_generation is not None and seen_generation != cookie_generation:
            return  # someone else already rotated since this request went out; retry with the new cookie
        if mark_exhausted and active_account:
            pool.mark_exhausted(active_account["username"], USAGE_CRAWL)
        attempted = {active_account["username"]} if active_account else set()

        while True:
            next_account = pool.rotate(USAGE_CRAWL, active_account["username"], require_cookie=False)
            if not next_account:
                cloudflare_challenged = True
                raise RuntimeError(f"{reason} — no more accounts to rotate")

            next_username = next_account["username"]
            if next_username in attempted:
                cloudflare_challenged = True
                raise RuntimeError(f"{reason} — no usable account after rotation")
            attempted.add(next_username)

            if not next_account.get("cookie"):
                info(f"{next_username} has no crawl cookie — capturing via Selenium")
                creds = pool.get_credentials(next_username)
                token_id = account_login.first_token_id()
                account_login.capture_account(pool, next_username, creds["password"], token_id)
                next_account = pool.get_active(USAGE_CRAWL, pin=next_username)
                if not next_account or not next_account.get("cookie"):
                    warn(f"Failed to capture cookie for {next_username}; trying next account")
                    active_account = {"username": next_username}
                    continue

            active_account = next_account
            cookie = next_account["cookie"]
            user_agent = next_account.get("userAgent") or DEFAULT_USER_AGENT
            cookie_generation += 1
            warn(f"Rotated to {next_account['username']} ({reason})")
            return


def parse_bscscan_props(html):
    start = html.find('id="collapseProperties"')
    if start == -1:
        return []
    region = html[start : start + 8000]
    cards = region.split('<div class="col px-1 mb-2">')[1:]
    props = []
    for card in cards:
        name = re.search(r'text-info[^>]*title="([^"]*)"', card)
        value = re.search(r"text-dark[^>]*>([^<]*)</p>", card)
        rarity = re.search(r"Rarity:\s*([\d.]+%)", card)
        if name and value:
            props.append(
                {
                    "trait_type": decode_entities(name.group(1)),
                    "value": decode_entities(value.group(1)),
                    "rarity": rarity.group(1) if rarity else None,
                }
            )
    return props


def fetch_bscscan(token_id):
    # Retry on Cloudflare/rate-limits/503 until rotation exhausts the pool
    # (recursion replaced with a loop to avoid stack growth).
    while True:
        respect_throttle()
        with cookie_lock:
            headers = {"user-agent": user_agent, "cookie": cookie}
            generation = cookie_generation
        url = f"https://bscscan.com/nft/{CONTRACT}/{token_id}"
        try:
            html = with_retries(
                "bscscan",
                short_token(token_id),
                lambda: http_get(url, headers=headers),
                transient_codes=BSCSCAN_TRANSIENT_HTTP_CODES,
            )
        except urllib.error.HTTPError as error:
            if error.code == 503:
                rotate_cookie("HTTP 503", seen_generation=generation)
                continue
            if error.code == 429:
                # Still throttled after the per-request retries: back everyone
                # off, then continue on the next account instead of reporting
                # the token as a crawl error (which costs a Selenium refresh).
                note_throttled()
                rotate_cookie("HTTP 429", seen_generation=generation)
                continue
            raise RuntimeError(f"bscscan: HTTP {error.code}") from error
        except (OSError, http.client.HTTPException) as error:
            # URLError, timeouts, SSL EOF, connection resets all subclass OSError.
            raise RuntimeError(f"bscscan: {describe_error(error)}") from error

        if 'id="collapseProperties"' not in html:
            if is_cloudflare_html(html):
                rotate_cookie("Cloudflare challenge", seen_generation=generation)
                continue
            if is_rate_limited_text(html):
                rotate_cookie("rate limit", mark_exhausted=True, seen_generation=generation)
                continue
            # Keep the title so the log tells a throttle page from a token that
            # simply has no properties on BscScan yet.
            title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            title_text = re.sub(r"\s+", " ", title.group(1)).strip()[:80] if title else "no title"
            raise RuntimeError(f"bscscan: page missing properties (title: {title_text})")
        return parse_bscscan_props(html)


def eth_call(data, label=""):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": CONTRACT, "data": data}, "latest"],
    }
    try:
        result = with_retries("rpc", label, lambda: http_post_json(RPC_URL, payload))
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise RuntimeError(f"rpc: {describe_error(error)}") from error
    if result.get("error"):
        message = result["error"].get("message", "")
        short = ":".join(message.split(":")[:2])
        raise RuntimeError(f"rpc: tokenURI revert: {short}")
    return result["result"]


def decode_abi_string(hex_result):
    raw = bytes.fromhex(hex_result[2:])
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode("utf-8")


def fetch_token_uri_attrs(token_id):
    label = short_token(token_id)
    hex_id = format(int(token_id), "x").zfill(64)
    uri = decode_abi_string(eth_call("0xc87b56dd" + hex_id, label=label))
    url = uri.replace("ipfs://", "https://ipfs.io/ipfs/")
    try:
        body = with_retries("metadata", label, lambda: http_get(url))
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError(f"metadata: {describe_error(error)}") from error
    try:
        meta = json.loads(body)
    except ValueError as error:
        raise RuntimeError(f"metadata: invalid JSON ({error})") from error
    attributes = meta.get("attributes") if isinstance(meta, dict) else None
    return attributes if isinstance(attributes, list) else []


def _is_blank(value):
    return value is None or str(value).strip() == ""


def _trait_map(items):
    result = {}
    for item in items:
        if not isinstance(item, dict) or item.get("trait_type") is None:
            continue
        result[norm(item["trait_type"])] = item.get("value")
    return result


def diff(bscscan_props, meta_attrs):
    b_map = _trait_map(bscscan_props)
    m_map = _trait_map(meta_attrs)
    keys = set(b_map) | set(m_map)
    diffs = []

    for key in keys:
        in_b = key in b_map
        in_m = key in m_map
        b_val = b_map.get(key)
        m_val = m_map.get(key)
        if in_b and in_m:
            if norm(b_val) == norm(m_val):
                if b_val != m_val:
                    diffs.append(
                        {"trait": key, "kind": "display_diff", "bscscan": b_val, "metadata": m_val}
                    )
            else:
                diffs.append(
                    {"trait": key, "kind": "value_diff", "bscscan": b_val, "metadata": m_val}
                )
        elif in_m:
            if _is_blank(m_val):
                continue  # BscScan never renders an empty trait; refreshing can't change that
            diffs.append({"trait": key, "kind": "missing_on_bscscan", "metadata": m_val})
        else:
            diffs.append({"trait": key, "kind": "missing_in_metadata", "bscscan": b_val})

    drift = [item for item in diffs if item["kind"] != "display_diff"]
    status = "in_sync" if not drift else "out_of_sync"
    return {"status": status, "diffs": diffs}


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as file:
            return json.load(file)
    return {}


def save_progress(progress):
    os.makedirs(CRAWL_OUTPUT_DIR, exist_ok=True)
    with open(PROGRESS_FILE, "w") as file:
        json.dump(progress, file, indent=2)


def compare_one(token_id):
    try:
        bscscan_props = fetch_bscscan(token_id)
        meta_attrs = fetch_token_uri_attrs(token_id)
        result = diff(bscscan_props, meta_attrs)
        return {
            "status": result["status"],
            "diffCount": len(result["diffs"]),
            "diffs": result["diffs"],
            "at": now_iso(),
        }
    except Exception as error:
        # rotate_cookie sets cloudflare_challenged before raising; nothing to do here.
        return {"status": "error", "error": str(error), "at": now_iso()}


def parse_args():
    parser = argparse.ArgumentParser(description="Compare BscScan cached NFT props vs on-chain metadata.")
    parser.add_argument("--reset", action="store_true", help="Wipe progress + report before starting")
    limit_env = os.environ.get("LIMIT")
    default_limit = int(limit_env) if limit_env else None
    parser.add_argument("--limit", type=int, default=default_limit)
    return parser.parse_args()


def main():
    args = parse_args()
    reset = args.reset or os.environ.get("RESET", "").lower() in ("1", "true")

    os.makedirs(CRAWL_OUTPUT_DIR, exist_ok=True)
    banner("Crawl — BscScan vs on-chain metadata")

    if reset:
        progress_file, report_file = reset_compare_files()
        ok(f"Reset {os.path.basename(progress_file)} + {os.path.basename(report_file)}")

    init_account()
    all_ids = [str(token_id) for token_id in load_token_ids(TOKEN_IDS_FILE)]
    limit = args.limit if args.limit is not None else len(all_ids)
    token_ids = all_ids[:limit]
    progress = load_progress()

    remaining = [
        token_id for token_id in token_ids if token_id not in progress or progress[token_id].get("status") == "error"
    ]
    summary("Run config", [
        ("Total tokens", len(token_ids)),
        ("Already done", len(token_ids) - len(remaining)),
        ("Remaining", len(remaining)),
        ("Concurrency", CONCURRENCY),
        ("RPC", RPC_URL),
    ])

    done = 0
    for start in range(0, len(remaining), CONCURRENCY):
        batch = remaining[start : start + CONCURRENCY]
        results = {}
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {executor.submit(compare_one, token_id): token_id for token_id in batch}
            for future in as_completed(futures):
                token_id = futures[future]
                results[token_id] = future.result()

        for token_id in batch:
            entry = results[token_id]
            progress[token_id] = entry
            done += 1
            label = short_token(token_id)
            if entry["status"] == "in_sync":
                ok(f"{label}  in_sync", indent=2)
            elif entry["status"] == "error":
                fail(f"{label}  {entry.get('error', 'error')}", indent=2)
            else:
                warn(f"{label}  out_of_sync ({entry.get('diffCount', 0)} diffs)", indent=2)

        save_progress(progress)
        info(f"Batch done — {done}/{len(remaining)} tokens", indent=2)
        if cloudflare_challenged:
            fail("Cloudflare challenge — stopping. Re-login: python3 accounts/login.py --account " + active_account["username"])
            break
        if start + CONCURRENCY < len(remaining):
            time.sleep(DELAY_SECONDS)

    vals = list(progress.items())
    count = lambda status: sum(1 for _, entry in vals if entry.get("status") == status)
    out_of_sync = [
        {"tokenId": token_id, "diffs": entry.get("diffs", [])}
        for token_id, entry in vals
        if entry.get("status") == "out_of_sync"
    ]
    errors = [
        {"tokenId": token_id, "error": entry.get("error", "")}
        for token_id, entry in vals
        if entry.get("status") == "error"
    ]

    with open(REPORT_FILE, "w") as file:
        json.dump({"outOfSync": out_of_sync, "errors": errors}, file, indent=2)

    summary("Crawl results", [
        ("in_sync", count("in_sync")),
        ("out_of_sync", count("out_of_sync")),
        ("errors", count("error")),
        ("Progress file", os.path.basename(PROGRESS_FILE)),
        ("Report file", os.path.basename(REPORT_FILE)),
    ])
    kv("Report contents", f"{len(out_of_sync)} out_of_sync, {len(errors)} errors")


if __name__ == "__main__":
    main()
