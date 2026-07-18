import datetime
from typing import Any, Optional


def bold(s: str) -> str:
    return f"*{s}*"


def code(s: str) -> str:
    return f"`{s}`"


def italic(s: str) -> str:
    return f"_{s}_"


def link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def fmt_balance(value: float) -> str:
    return f"${value:,.2f}"


def fmt_pnl(value: float) -> str:
    return f"{value:+,.2f}"


def fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def key_val(key: str, val: str) -> str:
    return f"{key}: {val}"


def listify(items: list[str], bullet: str = "•") -> str:
    return "\n".join(f"{bullet} {i}" for i in items)


def time_ago(ts: Optional[str]) -> str:
    if not ts:
        return "N/A"
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(ts)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return ts

    now = datetime.datetime.now(datetime.timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def time_ago_short(ts: Optional[str]) -> str:
    """Compact version: now / Xm / Xh / Xd."""
    ago = time_ago(ts)
    return ago


def dynamic_precision(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 6
    return 8


def fmt_price(price: float) -> str:
    d = dynamic_precision(price)
    return f"{price:.{d}f}"


def fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def fmt_holding(seconds: float) -> str:
    if seconds <= 0:
        return "0s"

    seconds = int(round(seconds))

    if seconds < 60:
        return f"{seconds}s"

    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m" if secs == 0 else f"{mins}m {secs}s"

    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h" if mins == 0 else f"{hours}h {mins:02d}m"

    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours:02d}h"


def fmt_compact_number(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def fmt_pf(value: float) -> str:
    import math
    if math.isinf(value):
        return "\u221e"
    if math.isnan(value):
        return "N/A"
    return f"{value:.2f}"
