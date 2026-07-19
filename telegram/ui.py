"""Telegram UI Design System — shared formatting primitives.

All Telegram output uses these helpers for a consistent, premium,
mobile-first appearance.

Design Language
───────────────
Separator: ━━━━━━━━━━━━━━━━━━
Header:    🤖 ZetBot AI
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Timezone ──────────────────────────────────────────────────────────

_WIB = timezone(timedelta(hours=7))

SEPARATOR = "━" * 18


def wib_now() -> str:
    """Return current time formatted as WIB (Asia/Jakarta)."""
    return datetime.now(_WIB).strftime("%d %b %Y\n%H:%M WIB")


def wib_datetime(ts: Optional[float] = None) -> str:
    """Format a UTC timestamp (or epoch float) to WIB datetime string.

    Returns: ``"19 Jul 2026 09:40 WIB"``
    """
    if ts is None:
        dt = datetime.now(_WIB)
    else:
        dt = datetime.fromtimestamp(ts, tz=_WIB)
    return dt.strftime("%d %b %Y %H:%M WIB")


def wib_time(ts: Optional[float] = None) -> str:
    """Format a UTC timestamp (or epoch float) to WIB time-only."""
    if ts is None:
        dt = datetime.now(_WIB)
    else:
        dt = datetime.fromtimestamp(ts, tz=_WIB)
    return dt.strftime("%H:%M WIB")


def wib_short(ts: Optional[float] = None) -> str:
    """Format a UTC timestamp to short WIB (HH:MM WIB)."""
    return wib_time(ts)


# ── Layout primitives ─────────────────────────────────────────────────

def header() -> str:
    """Standard bot header block."""
    return f"{SEPARATOR}\n🤖 *ZetBot AI*\n{SEPARATOR}"


def section(title: str) -> str:
    """Section divider with title."""
    return f"\n*{title}*"


def field(label: str, value: str) -> str:
    """Single label–value pair, one line."""
    return f"{label} {value}"


def kv(key: str, val: str) -> str:
    """Key: value pair."""
    return f"{key}: `{val}`"


# ── Progress bars ─────────────────────────────────────────────────────

def progress_bar(
    value: float,
    maximum: float = 100.0,
    length: int = 10,
) -> str:
    """Render a text-based progress bar.

    Example: ████████░░
    """
    if maximum <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, value / maximum))
    filled = round(ratio * length)
    empty = length - filled
    return "█" * filled + "░" * empty


def confidence_bar(value: float) -> str:
    """Confidence as a progress bar with percentage.

    Example: ████████░░ 83%
    """
    pct = max(0.0, min(100.0, value))
    bar = progress_bar(pct, 100.0, 10)
    return f"{bar} {pct:.0f}%"


def exposure_bar(pct: float) -> str:
    """Exposure as a progress bar with percentage."""
    bar = progress_bar(pct, 100.0, 10)
    return f"{bar} {pct:.0f}%"


# ── Status indicators ─────────────────────────────────────────────────

def status_icon(ok: bool) -> str:
    """Green circle if ok, red circle if not."""
    return "🟢" if ok else "🔴"


def status_dot(ok: bool, label: str) -> str:
    """Status line: 🟢 Label"""
    return f"{status_icon(ok)} {label}"


def pnl_emoji(value: float) -> str:
    """🟢 for profit, 🔴 for loss."""
    return "🟢" if value >= 0 else "🔴"


# ── AI Insight ────────────────────────────────────────────────────────

_EXIT_INSIGHTS: dict[str, list[str]] = {
    "Take Profit": [
        "TP reached successfully.",
        "Target hit — profit secured.",
        "Exit at target level executed.",
    ],
    "Stop Loss": [
        "Stop Loss protected capital.",
        "Risk managed — loss capped.",
        "SL triggered to limit downside.",
    ],
    "Strategy Exit": [
        "Strategy signaled exit.",
        "Signal reversed — position closed.",
        "Trend shifted — orderly exit.",
    ],
}


def ai_insight(
    recommendation: str = "",
    reasons: Optional[list[str]] = None,
    trend: str = "",
    confidence: float = 0.0,
    is_buy: bool = True,
    exit_reason: str = "",
) -> str:
    """Generate a short AI insight from available strategy outputs.

    Maximum 2 sentences. Never fabricates data.
    """
    parts: list[str] = []

    # Exit-reason insights (highest priority for sell notifications)
    if not is_buy and exit_reason in _EXIT_INSIGHTS:
        parts.append(_EXIT_INSIGHTS[exit_reason][0])
    elif not is_buy and exit_reason:
        parts.append(f"{exit_reason} exit executed.")

    if trend and len(parts) < 2:
        trend_lower = trend.lower()
        if is_buy:
            if "up" in trend_lower or "bull" in trend_lower:
                parts.append("Trend remains bullish.")
            elif "down" in trend_lower or "bear" in trend_lower:
                parts.append("Trend shows bearish pressure.")
            else:
                parts.append(f"Trend: {trend}.")
        else:
            if "up" in trend_lower or "bull" in trend_lower:
                parts.append("Trend still bullish — momentum weakening.")
            elif "down" in trend_lower or "bear" in trend_lower:
                parts.append("Downtrend confirmed.")
            else:
                parts.append(f"Trend: {trend}.")

    if reasons and len(parts) < 2:
        clean = [r for r in reasons if r and r != "Paper trade executed"]
        if clean:
            parts.append(clean[0] + ("." if not clean[0].endswith(".") else ""))

    if not parts:
        if is_buy:
            parts.append("Signal analysis complete.")
        else:
            parts.append("Position closed per strategy rules.")

    text = " ".join(parts[:2])
    return text


# ── Message assembly ──────────────────────────────────────────────────

def build_message(*blocks: str) -> str:
    """Join message blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


def empty_positions() -> str:
    """Standard 'no open positions' message."""
    return f"{header()}\n\nNo open positions."
