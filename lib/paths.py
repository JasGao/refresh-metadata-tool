import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ACCOUNTS_DIR = os.path.join(PROJECT_ROOT, "accounts")
ACCOUNTS_FILE = os.path.join(ACCOUNTS_DIR, "accounts.json")
ACCOUNT_STATE_FILE = os.path.join(ACCOUNTS_DIR, "state.json")

CRAWL_DIR = os.path.join(PROJECT_ROOT, "crawl")
TOKEN_IDS_FILE = os.path.join(PROJECT_ROOT, "tokens.csv")
CRAWL_OUTPUT_DIR = os.path.join(CRAWL_DIR, "output")
CRAWL_PROGRESS_FILE = os.path.join(CRAWL_OUTPUT_DIR, "progress.json")
CRAWL_REPORT_FILE = os.path.join(CRAWL_OUTPUT_DIR, "report.json")

