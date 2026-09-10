import json
import os

from lib.paths import CRAWL_REPORT_FILE

# Crawl errors that a BscScan refresh can never fix. "tokenURI revert" means the
# token no longer exists on-chain (burned/unregistered); it was being sent to the
# Selenium refresh every day (14 per run on 2026-09-07/08) for nothing.
NOT_REFRESHABLE_MARKERS = ("tokenuri revert",)


def _token_ids(entries):
    return [entry["tokenId"] for entry in entries if entry.get("tokenId")]


def is_refreshable_error(entry):
    message = str(entry.get("error", "")).lower()
    return not any(marker in message for marker in NOT_REFRESHABLE_MARKERS)


def load_report(report_path=CRAWL_REPORT_FILE):
    if not os.path.exists(report_path):
        return {"outOfSync": [], "errors": []}
    with open(report_path, "r") as file:
        return json.load(file)


def refresh_targets_from_report(report):
    """Out-of-sync tokens plus refreshable crawl errors (deduped; out-of-sync first)."""
    seen = set()
    tokens = []
    errors = [entry for entry in report.get("errors", []) if is_refreshable_error(entry)]
    for token_id in _token_ids(report.get("outOfSync", [])) + _token_ids(errors):
        if token_id not in seen:
            seen.add(token_id)
            tokens.append(token_id)
    return tokens


def refresh_target_counts(report_path=CRAWL_REPORT_FILE):
    report = load_report(report_path)
    tokens = refresh_targets_from_report(report)
    out_of_sync_ids = set(_token_ids(report.get("outOfSync", [])))
    targets = set(tokens)
    skipped = [
        entry["tokenId"]
        for entry in report.get("errors", [])
        if entry.get("tokenId") and not is_refreshable_error(entry) and entry["tokenId"] not in targets
    ]
    return {
        "out_of_sync": len([token_id for token_id in tokens if token_id in out_of_sync_ids]),
        "errors": len([token_id for token_id in tokens if token_id not in out_of_sync_ids]),
        "skipped": len(set(skipped)),
        "total": len(tokens),
    }


def load_refresh_token_ids(report_path=CRAWL_REPORT_FILE):
    return refresh_targets_from_report(load_report(report_path))
