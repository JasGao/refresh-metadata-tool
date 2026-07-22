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
import json
import os
import re
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
from lib.paths import CRAWL_OUTPUT_DIR, PROJECT_ROOT, migrate_legacy_paths
from lib.reset_compare import PROGRESS_FILE, REPORT_FILE, reset_compare_files
from lib.tokenids import TOKEN_IDS_FILE, load_token_ids

CONTRACT = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
RPC_URL = os.environ.get("BNB_MAINNET_RPC_URL", "https://bsc-dataseed.binance.org")
CONCURRENCY = 10
DELAY_SECONDS = 1.5

pool = AccountPool()
active_account = None
cookie = ""
user_agent = DEFAULT_USER_AGENT
cloudflare_challenged = False
cookie_lock = threading.RLock()


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
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


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


def rotate_cookie(reason, mark_exhausted=False):
    global active_account, cookie, user_agent, cloudflare_challenged
    with cookie_lock:
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
        with cookie_lock:
            headers = {"user-agent": user_agent, "cookie": cookie}
        url = f"https://bscscan.com/nft/{CONTRACT}/{token_id}"
        try:
            html = http_get(url, headers=headers)
        except urllib.error.HTTPError as error:
            if error.code == 503:
                rotate_cookie("HTTP 503")
                continue
            raise RuntimeError(f"BSCScan HTTP {error.code}") from error

        if 'id="collapseProperties"' not in html:
            if is_cloudflare_html(html):
                rotate_cookie("Cloudflare challenge")
                continue
            if is_rate_limited_text(html):
                rotate_cookie("rate limit", mark_exhausted=True)
                continue
            raise RuntimeError("BSCScan page missing properties (throttled?)")
        return parse_bscscan_props(html)


def eth_call(data):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": CONTRACT, "data": data}, "latest"],
    }
    result = http_post_json(RPC_URL, payload)
    if result.get("error"):
        message = result["error"].get("message", "")
        short = ":".join(message.split(":")[:2])
        raise RuntimeError(f"tokenURI revert: {short}")
    return result["result"]


def decode_abi_string(hex_result):
    raw = bytes.fromhex(hex_result[2:])
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode("utf-8")


def fetch_token_uri_attrs(token_id):
    hex_id = format(int(token_id), "x").zfill(64)
    uri = decode_abi_string(eth_call("0xc87b56dd" + hex_id))
    url = uri.replace("ipfs://", "https://ipfs.io/ipfs/")
    meta = json.loads(http_get(url))
    attributes = meta.get("attributes")
    return attributes if isinstance(attributes, list) else []


def diff(bscscan_props, meta_attrs):
    b_map = {norm(item["trait_type"]): item["value"] for item in bscscan_props}
    m_map = {norm(item["trait_type"]): item["value"] for item in meta_attrs}
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
    global cloudflare_challenged
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
        message = str(error)
        if "Cloudflare challenge" in message or "no more accounts to rotate" in message:
            cloudflare_challenged = True
        return {"status": "error", "error": message, "at": now_iso()}


def parse_args():
    parser = argparse.ArgumentParser(description="Compare BscScan cached NFT props vs on-chain metadata.")
    parser.add_argument("--reset", action="store_true", help="Wipe progress + report before starting")
    limit_env = os.environ.get("LIMIT")
    default_limit = int(limit_env) if limit_env else None
    parser.add_argument("--limit", type=int, default=default_limit)
    return parser.parse_args()


def main():
    migrate_legacy_paths()
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
