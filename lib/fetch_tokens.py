"""Fetch tokens.csv from a Google Sheet (CSV export of a public sheet).

Automates step 1 of the workflow: pull the current token list from a shared
Google Sheet and overwrite tokens.csv with its contents.

The sheet must be shared as "Anyone with the link" (viewer is enough); we hit
the public CSV export endpoint, so no Google API credentials are required.

Configuration (required):
  TOKENS_SHEET_URL  full spreadsheet URL (id + gid are parsed out of it)

Optional overrides:
  TOKENS_SHEET_ID   spreadsheet id (overrides the one parsed from the URL)
  TOKENS_SHEET_GID  worksheet gid (default: parsed from URL, else 0)
"""

import os
import re
import urllib.parse

import requests

from lib.env import load_dotenv
from lib.paths import TOKEN_IDS_FILE

HEADER = "tokenId"
REQUEST_TIMEOUT = 30


def parse_sheet_ref(url):
    """Extract (sheet_id, gid) from a Google Sheets URL."""
    id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not id_match:
        raise ValueError(f"Could not find a spreadsheet id in URL: {url}")
    sheet_id = id_match.group(1)

    gid = "0"
    gid_match = re.search(r"[?#&]gid=([0-9]+)", url)
    if gid_match:
        gid = gid_match.group(1)
    return sheet_id, gid


def export_csv_url(sheet_id, gid="0"):
    query = urllib.parse.urlencode({"format": "csv", "gid": gid})
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?{query}"


def _resolve_ref():
    load_dotenv()
    url = os.environ.get("TOKENS_SHEET_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Set TOKENS_SHEET_URL in .env (Google Sheet link shared as "
            "'Anyone with the link' — viewer is enough)."
        )
    sheet_id, gid = parse_sheet_ref(url)
    sheet_id = os.environ.get("TOKENS_SHEET_ID", sheet_id).strip() or sheet_id
    gid = os.environ.get("TOKENS_SHEET_GID", gid).strip() or gid
    return sheet_id, gid


def _normalize_ids(text):
    """Return the cleaned tokenId lines from the fetched CSV body."""
    ids = []
    for raw in text.splitlines():
        # Google exports a single-column sheet with no trailing comma, but be
        # defensive in case the sheet grows extra columns.
        value = raw.split(",")[0].strip().strip('"')
        if not value or value == HEADER:
            continue
        ids.append(value)
    return ids


def fetch_tokens(dest=None):
    """Download the sheet and overwrite tokens.csv. Returns the token count.

    A .bak copy of the previous file is kept so a bad fetch is recoverable.
    An empty sheet (header only) writes tokens.csv with just the header and returns 0.
    Raises RuntimeError if the HTTP response is not a usable CSV export.
    """
    dest = dest or TOKEN_IDS_FILE
    sheet_id, gid = _resolve_ref()
    url = export_csv_url(sheet_id, gid)

    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(
            f"Google Sheet fetch failed (HTTP {response.status_code}). "
            "Is the sheet shared as 'Anyone with the link'?"
        )

    content_type = response.headers.get("Content-Type", "")
    if "text/csv" not in content_type:
        # Google returns an HTML sign-in page for private sheets.
        raise RuntimeError(
            f"Expected CSV but got '{content_type or 'unknown'}'. "
            "The sheet is probably not publicly shared."
        )

    ids = _normalize_ids(response.text)
    if os.path.exists(dest):
        backup = f"{dest}.bak"
        with open(dest, "r") as src, open(backup, "w") as bak:
            bak.write(src.read())

    tmp = f"{dest}.tmp"
    with open(tmp, "w") as file:
        file.write(HEADER + "\n")
        if ids:
            file.write("\n".join(ids) + "\n")
    os.replace(tmp, dest)

    return len(ids), sheet_id, gid


if __name__ == "__main__":
    count, sheet_id, gid = fetch_tokens()
    print(f"Wrote {count} token ids from sheet {sheet_id} (gid={gid}) to {TOKEN_IDS_FILE}")
