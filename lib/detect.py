"""Shared page-state detection for Cloudflare challenges and rate limits.

Used by crawl (HTML strings), refresh, and login (Selenium page source).
"""

import re

# Only markers that appear on an actual challenge/block page. Do NOT match a bare
# "challenge-platform": Cloudflare's bot-management beacon
# (/cdn-cgi/challenge-platform/scripts/jsd/main.js) is injected into ordinary
# BscScan pages, and matching it made healthy NFT pages look like challenges —
# which rotated accounts, discarded recovered sessions and forced Turnstile
# logins (see logs 2026-09-07/08). Real challenge pages load the orchestrator
# from challenge-platform/h/ and carry the _cf_chl_opt payload.
CLOUDFLARE_RE = re.compile(
    r"<title>\s*Just a moment|cf-mitigated|Attention Required!|"
    r"challenge-platform/h/b/orchestrate|_cf_chl_opt|cf-chl-|"
    r"Enable JavaScript and cookies to continue|Checking your browser before|"
    r"Verify you are human",
    re.I,
)

RATE_LIMIT_PHRASES = (
    "surpassed the daily limit",
    "too many requests",
    "rate limit",
)


def is_cloudflare_html(html):
    """True if the page HTML looks like a Cloudflare interstitial or block page."""
    return bool(CLOUDFLARE_RE.search(html or ""))


def is_rate_limited_text(text):
    """True if the page text signals a BscScan daily/rate limit."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in RATE_LIMIT_PHRASES)
