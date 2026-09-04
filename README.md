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

Results: `crawl/output/report.json` → `outOfSync` / `errors`  
Logs: `logs/daily-*.log`
