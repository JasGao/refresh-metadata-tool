def _out(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def banner(title):
    width = max(50, len(title) + 6)
    _out(f"\n{'═' * width}")
    _out(f"  {title}")
    _out(f"{'═' * width}")


def step(number, title):
    _out(f"\n── Step {number}: {title} {'─' * max(0, 40 - len(title))}")


def substep(title):
    _out(f"\n  ▸ {title}")


def kv(key, value, indent=2):
    _out(f"{' ' * indent}{key:<18} {value}")


def ok(message, indent=2):
    _out(f"{' ' * indent}✓ {message}")


def warn(message, indent=2):
    _out(f"{' ' * indent}⚠ {message}")


def fail(message, indent=2):
    _out(f"{' ' * indent}✗ {message}")


def info(message, indent=2):
    _out(f"{' ' * indent}· {message}")


def progress(current, total, detail, indent=2):
    _out(f"{' ' * indent}[{current}/{total}] {detail}")


def summary(title, rows):
    _out(f"\n── {title} {'─' * max(0, 36 - len(title))}")
    for key, value in rows:
        kv(key, value)


def short_token(token_id, tail=10):
    text = str(token_id)
    return text if len(text) <= tail + 1 else f"…{text[-tail:]}"
