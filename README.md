# BscScan NFT Metadata Tools

Compares BscScan’s cached NFT metadata with on-chain data, then refreshes tokens that are out of sync.

**Contract:** `0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30` (Renaiss)

Each run:

1. Pulls the token list from your Google Sheet → `tokens.csv`
2. Logs in → crawls → refreshes stale tokens
3. Writes results to `crawl/output/report.json` and logs to `logs/`

Edit tokens in the **Google Sheet** (share as “Anyone with the link” — viewer is enough). Set the sheet URL in `.env` as `TOKENS_SHEET_URL`. You don’t need to edit `tokens.csv` by hand.

---

## Setup (once)

```bash
pip install undetected-chromedriver selenium requests
cp .env.example .env          # set BSCSCAN_PASSWORD + TOKENS_SHEET_URL
```

Put usernames in `accounts/accounts.json` (password comes from `.env`).

Warm Chrome once (screen unlocked — complete captcha if asked):

```bash
BSCSCAN_MANUAL_LOGIN=1 python3 accounts/login.py --account refresh1
```

---

## Run manually

```bash
python3 run_workflow.py
```

Useful flags:

| Flag | Meaning |
|------|---------|
| `--skip-fetch` | Keep local `tokens.csv` (don’t pull the sheet) |
| `--skip-login` | Reuse existing cookies |
| `--crawl-only` | Skip the refresh step |
| `--skip-reset` | Resume an interrupted crawl |

---

## Schedule daily (macOS)

```bash
scripts/install_schedule.sh 09:30          # change time as needed
launchctl kickstart -k gui/$(id -u)/com.renaiss.refresh.daily   # test now
tail -f logs/daily-*.log
scripts/uninstall_schedule.sh              # remove schedule
```

**Mac must stay awake, logged in, and unlocked** at run time. Use Amphetamine with **“Allow system sleep when display is closed” unchecked** if you close the lid. Prefer AC power.

After each run (scheduled or manual), `run_workflow.py` commits and pushes the log to the remote repo. Set `REFRESH_GIT_PUSH=0` in `scripts/schedule.local.env` to disable.

Optional overrides: create `scripts/schedule.local.env` (gitignored), e.g. `BSCSCAN_ACCOUNT=refresh1`.

---

## Project layout

```
refresh/
├── run_workflow.py          # main entry
├── .env                     # BSCSCAN_PASSWORD + TOKENS_SHEET_URL (gitignored)
├── accounts/accounts.json   # usernames (gitignored)
├── accounts/state.json      # cookies (gitignored)
├── tokens.csv               # auto-pulled from the sheet each run
├── crawl/output/            # progress + report
├── logs/                    # daily run logs
├── scripts/                 # launchd schedule helpers
├── crawl/compare.py
└── refresh-metadata/refresh.py
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Turnstile not ready` | Warm profile with `BSCSCAN_MANUAL_LOGIN=1`; keep screen unlocked |
| `No module named 'undetected_chromedriver'` | `pip install undetected-chromedriver selenium requests` for the same `python3` the schedule uses |
| Chrome dies with lid closed | Uncheck Amphetamine “Allow system sleep when display is closed”, or leave lid open |
| Sheet fetch fails | Set `TOKENS_SHEET_URL` in `.env`; sheet must be “Anyone with the link” (viewer OK) |
| Need more accounts | Add usernames to `accounts/accounts.json` |
| Crawl interrupted | `python3 run_workflow.py --skip-reset --skip-login` |
| `Another workflow run is already in progress` | A previous run (scheduled or manual) is still going. Two runs share the Chrome profiles and kill each other's browser, so the second one now exits with code 2 instead. Wait, or kill the pid shown. |
| Sheet fetch fails but the run continues | Since 2026-09-10 the sheet is retried 3x and, if still unreachable, the run uses the `tokens.csv` from the previous run (logged as a warning). |
| Refresh log ends with `Refresh stopped early` | The pass hit `BSCSCAN_MAX_CONSECUTIVE_FAILURES` failures in a row (or the network never came back). Fix the cause, then `refresh-metadata/refresh.py --from-token …suffix` for the "Not attempted" tokens. |
| Refresh stalls for minutes | Lower `BSCSCAN_COMMAND_TIMEOUT` / `BSCSCAN_PAGE_LOAD_TIMEOUT` (see below) |
| Stuck after `Chrome version_main NNN`, no browser window | undetected-chromedriver was re-downloading chromedriver (18 MB, no timeout) on every browser start. Fixed 2026-09-07: the cached binary is reused; a download only happens after a Chrome update and is capped by `BSCSCAN_DRIVER_DOWNLOAD_TIMEOUT`. |

### Refresh timing knobs

Set these in `scripts/schedule.local.env` or the shell.

| Env | Default | Meaning |
|-----|---------|---------|
| `BSCSCAN_COMMAND_TIMEOUT` | `90` | Max seconds any single chromedriver command may block. Selenium's own default is unlimited, so a wedged Chrome used to hang the run silently. |
| `BSCSCAN_PAGE_LOAD_TIMEOUT` | `35` | Max seconds for a page load before the load is stopped. |
| `BSCSCAN_PAGE_LOAD_STRATEGY` | `eager` | `eager` returns at DOMContentLoaded instead of waiting for ads/trackers. Use `normal` for the old behaviour. |
| `BSCSCAN_NAVIGATE_ATTEMPTS` | `2` | Re-open the NFT page this many times on a page-load timeout before restarting Chrome. |
| `BSCSCAN_BROWSER_RESTART_EVERY` | `18` | Proactive Chrome restart interval, in tokens. `0` disables. |
| `BSCSCAN_REMEMBER_ME` | `1` | Tick "Remember & Auto Login" when logging in. BscScan then sets 45-day `bscscan_autologin`/`bscscan_pwd`/`bscscan_userid` cookies, so a Chrome restart, a next-day run, or a cookie restore on another machine logs in **without Turnstile**. Verified 2026-09-10. `0` restores the old session-only login. |
| `BSCSCAN_STRICT_USERNAME` | unset | `1` makes an unverifiable username fail the session check (forces a full Turnstile login). Off by default — Chrome profiles are per-account. |
| `BSCSCAN_TURNSTILE_ABSENT_GRACE` | `12` | If no Turnstile widget has rendered after this many seconds, reload instead of polling out `BSCSCAN_CAPTCHA_WAIT`. |
| `BSCSCAN_CAPTCHA_WAIT` | `120` (in `run_daily.sh`) | Max wait for a Turnstile token *when the widget is actually on the page*. |
| `BSCSCAN_LOGIN_RETRIES` | `5` (in `run_daily.sh`) | Login attempts before giving up. |
| `BSCSCAN_LOGIN_STEP_DELAY` | `2.5` | Settle pause after each navigation. |
| `BSCSCAN_DRIVER_DOWNLOAD_TIMEOUT` | `120` | Max seconds for a chromedriver download when the cached binary no longer matches Chrome. |
| `BSCSCAN_MAX_CONSECUTIVE_FAILURES` | `8` | A single failed token no longer aborts the run (2026-09-10). Only this many failures *in a row* stop the pass; the rest are listed as "Not attempted" and the run exits 1. |
| `BSCSCAN_RETRY_FAILED_PASSES` | `1` | After the main pass, retry the failed tokens this many times with a fresh Chrome. `0` disables. |
| `BSCSCAN_RETRY_FAILED_DELAY` | `30` | Pause before a retry pass, seconds. |
| `BSCSCAN_NETWORK_WAIT` | `600` | After two failures in a row, if bscscan.com is unreachable, wait up to this long for the network to return instead of failing every remaining token. |

### Crawl network knobs

| Env | Default | Meaning |
|-----|---------|---------|
| `CRAWL_CONCURRENCY` | `50` | Tokens fetched in parallel per batch. Was `10` until 2026-09-07; 50 tested clean and crawls about 3x faster. Drop to `25` if `bscscan:`/`metadata:` timeouts spike; do not go to 100 — the metadata host is the bottleneck and BscScan bot detection gets riskier. |
| `CRAWL_BATCH_DELAY` | `1.5` | Pause between batches, seconds. |
| `CRAWL_RETRIES` | `3` | Attempts per HTTP call for transient errors (timeouts, resets, SSL EOF, HTTP 429/5xx). Before, one timeout put the token in the report as a crawl error and cost a Selenium refresh. |
| `CRAWL_RETRY_DELAY` | `2` | Seconds between attempts (grows linearly). |
| `CRAWL_HTTP_TIMEOUT` | `30` | Per-request timeout for BscScan, RPC, and metadata fetches. |
| `CRAWL_THROTTLE_PAUSE` | `20` | After a BscScan HTTP 429 all workers pause this long, then the crawl continues on the next account instead of reporting the token as an error. |
| `TOKENS_SHEET_ATTEMPTS` | `3` | Google Sheet fetch attempts before falling back to the local `tokens.csv`. |

Crawl errors in `report.json` and the logs are prefixed with the stage that failed: `bscscan:`, `rpc:`, or `metadata:`. A `rpc: tokenURI revert` error means the token no longer exists on-chain; it stays in the report but is **not** sent to the refresh step (shown as "Not refreshable").

Account rotation is logged with its reason (`Rotated to refresh3 (Cloudflare challenge)`, `(rate limit)`, `(quota used up)`, `(login failed)`). In the crawl, one Cloudflare/429 event now causes exactly one rotation, not one per in-flight worker.

Session reuse always opens `/login` first (`accounts/login.py: autologin_via_login_page`). With remember-me cookies BscScan logs in there and redirects to My Account; opening My Account first instead makes the server *delete* the remembered cookies, which is why "Checking Chrome profile session" never succeeded before 2026-09-10. Expect `Session recovered from Chrome profile` / `Reused Chrome profile session — no Turnstile login needed` in the logs; a Turnstile login should now only happen the first time an account is used on a machine (or after ~45 days).

Cloudflare detection (`lib/detect.py`) only matches real challenge/block pages. Until 2026-09-10 it also matched Cloudflare's bot-management beacon that BscScan injects into ordinary pages, which made healthy NFT pages look like challenges — the cause of most surprise rotations, discarded sessions, and the 2026-09-07/08 crashes.

Login now logs its own duration (`✓ Login successful  refresh1  (18.3s)`) and the Turnstile solve time.

Each refresh line now logs its elapsed time (`✓ …1234567890  refresh clicked  (11.4s)`), so a stall is visible in `logs/`.

Results: `crawl/output/report.json` → `outOfSync` / `errors`  
Logs: `logs/daily-*.log`
