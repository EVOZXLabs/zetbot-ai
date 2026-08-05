import datetime
from typing import Any, Optional

# Characters with special meaning in Telegram's legacy Markdown parse
# mode. Any one of them inside DYNAMIC text (symbols, AI insight lines,
# error messages) makes the API reject the whole message with
# "can't parse entities" — so dynamic values are escaped via
# ``md_escape`` before interpolation.
_MD_SPECIALS = frozenset(r"\`*_{}[]()~#+-.=!|>")


def md_escape(s: Any) -> str:
    """Escape Telegram Markdown specials in *dynamic* text.

    Applies ONLY to dynamic values — never to strings that already
    contain our own Markdown markup (``*bold*``, ```code``, links).
    Over-escaping is harmless: a backslash-escaped special renders as
    the literal character, so this can never change what the user reads
    and can never trigger a parse rejection.
    """
    return "".join(f"\\{c}" if c in _MD_SPECIALS else c for c in str(s))


def bold(s: str) -> str:
    return f"*{s}*"


def code(s: str) -> str:
    return f"`{s}`"


def italic(s: str) -> str:
    return f"_{s}_"


def link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def fmt_balance(value: float, currency: str = "USDT") -> str:
    return f"{value:,.2f} {currency}"


def fmt_pnl(value: float, currency: str = "") -> str:
    if currency:
        return f"{value:+,.2f} {currency}"
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


def fmt_compact_number(value: float, currency: str = "USDT") -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B {currency}"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {currency}"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.2f}K {currency}"
    return f"{value:.2f} {currency}"


def fmt_pf(value: float) -> str:
    import math
    if math.isinf(value):
        return "\u221e"
    if math.isnan(value):
        return "N/A"
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
#  Hold-time helpers (shared by /summary, /performance, /history)
# ---------------------------------------------------------------------------


def parse_ts(s: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp (``Z`` suffix tolerated), else None."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def order_hold_seconds(
    order: dict[str, Any],
    entry_time_by_symbol: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[float]:
    """Hold duration of a closed order, or None when it cannot be known.

    A closing SELL order's own ``filled_at`` is the *exit* moment, not
    the entry — using it directly yields a meaningless "0s" hold.  The
    real entry time comes from the position record (``entry_time`` /
    ``opened_at``), looked up on the order itself first, then in the
    ``entry_time_by_symbol`` map (symbol → position record) supplied by
    the caller.  Returns None (no hold shown) when no entry record
    exists — better than a fabricated "0s".
    """
    exit_raw = order.get("exit_time") or order.get("closed_at") or ""
    exit_dt = parse_ts(exit_raw)
    if exit_dt is None:
        return None

    entry_raw = (
        order.get("entry_time")
        or order.get("opened_at")
        or (entry_time_by_symbol or {}).get(order.get("symbol", ""), {}).get("opened_at")
        or ""
    )
    entry_dt = parse_ts(entry_raw)
    if entry_dt is None:
        return None

    return max(0.0, (exit_dt - entry_dt).total_seconds())
