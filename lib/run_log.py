"""Capture workflow output to logs/ and push the log to git on exit."""

import os
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def fileno(self):
        return self.streams[0].fileno()


class RunLogContext:
    def __init__(self, log_path, log_file, orig_stdout, orig_stderr):
        self.log_path = log_path
        self._log_file = log_file
        self._orig_stdout = orig_stdout
        self._orig_stderr = orig_stderr

    def restore_streams(self):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        if self._log_file:
            self._log_file.close()
            self._log_file = None


_context = None


def setup_run_log():
    """Mirror stdout/stderr to logs/daily-*.log for every workflow run."""
    global _context
    if os.environ.get("REFRESH_SKIP_LOG_PUSH") == "1":
        return None
    if _context is not None:
        return _context

    log_path = os.environ.get("REFRESH_LOG_FILE")
    orig_stdout, orig_stderr = sys.stdout, sys.stderr

    os.makedirs(LOGS_DIR, exist_ok=True)
    if not log_path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(LOGS_DIR, f"daily-{stamp}.log")

    is_new = not os.path.exists(log_path) or os.path.getsize(log_path) == 0
    log_file = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

    if is_new:
        log_file.write(f"=== refresh run: {datetime.now()} ===\n")
        log_file.write(f"repo={SCRIPT_DIR}\n")
        log_file.write(f"python={sys.executable} ({sys.version.split()[0]})\n")
        log_file.write(f"args={' '.join(sys.argv[1:]) or '<full pipeline>'}\n")
        log_file.flush()

    sys.stdout = _Tee(orig_stdout, log_file)
    sys.stderr = _Tee(orig_stderr, log_file)

    _context = RunLogContext(log_path, log_file, orig_stdout, orig_stderr)
    return _context


def push_run_log(exit_code=0):
    """Commit and push the run log unless REFRESH_GIT_PUSH=0."""
    global _context
    if _context is None:
        return

    log_path = _context.log_path
    _context.restore_streams()
    _context = None

    if os.environ.get("REFRESH_GIT_PUSH", "1") == "0":
        return

    with open(log_path, "a", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"=== exit {exit_code} at {datetime.now()} ===\n")

    push_script = os.path.join(SCRIPT_DIR, "scripts", "push_daily_log.sh")
    if not os.path.isfile(push_script):
        print(f"push script missing: {push_script}", file=sys.stderr)
        return

    subprocess.run(
        ["bash", push_script, SCRIPT_DIR, log_path, str(exit_code), sys.executable],
        cwd=SCRIPT_DIR,
        check=False,
    )


def exit_code_from_system_exit(exc):
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1
