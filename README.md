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
| Refresh stalls for minutes | Lower `BSCSCAN_COMMAND_TIMEOUT` / `BSCSCAN_PAGE_LOAD_TIMEOUT` (see below) |

### Refresh timing knobs

Set these in `scripts/schedule.local.env` or the shell.

| Env | Default | Meaning |
|-----|---------|---------|
| `BSCSCAN_COMMAND_TIMEOUT` | `90` | Max seconds any single chromedriver command may block. Selenium's own default is unlimited, so a wedged Chrome used to hang the run silently. |
| `BSCSCAN_PAGE_LOAD_TIMEOUT` | `35` | Max seconds for a page load before the load is stopped. |
| `BSCSCAN_PAGE_LOAD_STRATEGY` | `eager` | `eager` returns at DOMContentLoaded instead of waiting for ads/trackers. Use `normal` for the old behaviour. |
| `BSCSCAN_NAVIGATE_ATTEMPTS` | `2` | Re-open the NFT page this many times on a page-load timeout before restarting Chrome. |
| `BSCSCAN_BROWSER_RESTART_EVERY` | `19` | Proactive Chrome restart interval, in tokens. `0` disables. |
| `BSCSCAN_STRICT_USERNAME` | unset | `1` makes an unverifiable username fail the session check (forces a full Turnstile login). Off by default — Chrome profiles are per-account. |
| `BSCSCAN_TURNSTILE_ABSENT_GRACE` | `12` | If no Turnstile widget has rendered after this many seconds, reload instead of polling out `BSCSCAN_CAPTCHA_WAIT`. |
| `BSCSCAN_CAPTCHA_WAIT` | `120` (in `run_daily.sh`) | Max wait for a Turnstile token *when the widget is actually on the page*. |
| `BSCSCAN_LOGIN_RETRIES` | `5` (in `run_daily.sh`) | Login attempts before giving up. |
| `BSCSCAN_LOGIN_STEP_DELAY` | `2.5` | Settle pause after each navigation. |

### Crawl network knobs

| Env | Default | Meaning |
|-----|---------|---------|
| `CRAWL_CONCURRENCY` | `50` | Tokens fetched in parallel per batch. Was `10` until 2026-09-07; 50 tested clean and crawls about 3x faster. Drop to `25` if `bscscan:`/`metadata:` timeouts spike; do not go to 100 — the metadata host is the bottleneck and BscScan bot detection gets riskier. |
| `CRAWL_BATCH_DELAY` | `1.5` | Pause between batches, seconds. |
| `CRAWL_RETRIES` | `3` | Attempts per HTTP call for transient errors (timeouts, resets, SSL EOF, HTTP 429/5xx). Before, one timeout put the token in the report as a crawl error and cost a Selenium refresh. |
| `CRAWL_RETRY_DELAY` | `2` | Seconds between attempts (grows linearly). |
| `CRAWL_HTTP_TIMEOUT` | `30` | Per-request timeout for BscScan, RPC, and metadata fetches. |

Crawl errors in `report.json` and the logs are prefixed with the stage that failed: `bscscan:`, `rpc:`, or `metadata:`.

Login now logs its own duration (`✓ Login successful  refresh1  (18.3s)`) and the Turnstile solve time.

Each refresh line now logs its elapsed time (`✓ …1234567890  refresh clicked  (11.4s)`), so a stall is visible in `logs/`.

Results: `crawl/output/report.json` → `outOfSync` / `errors`  
Logs: `logs/daily-*.log`
