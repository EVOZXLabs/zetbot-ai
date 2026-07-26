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
#
# Rule: never emit the same sentence for two different trades. Every
# insight is built from the actual numbers of *this* trade (result %,
# holding time) plus the real strategy reason, if one was supplied —
# never a static line picked at random from a fixed list.

_EXIT_LEAD: dict[str, str] = {
    "Take Profit": "TP reached",
    "Stop Loss": "Stop Loss triggered",
    "Strategy Exit": "Strategy signaled the exit",
}


def ai_insight(
    recommendation: str = "",
    reasons: Optional[list[str]] = None,
    trend: str = "",
    confidence: float = 0.0,
    is_buy: bool = True,
    exit_reason: str = "",
    pnl_pct: Optional[float] = None,
    holding_str: str = "",
) -> str:
    """Build a short, trade-specific insight line.

    Grounded in the actual outcome of this trade (result %, how long it
    was held, the real strategy reason) so two different trades never
    read as the same sentence. Never fabricates data — omits a detail
    entirely if it wasn't supplied.
    """
    if not is_buy:
        lead = _EXIT_LEAD.get(
            exit_reason, f"{exit_reason} exit" if exit_reason else "Position closed",
        )

        outcome: list[str] = []
        if pnl_pct is not None:
            direction = "up" if pnl_pct >= 0 else "down"
            outcome.append(f"{direction} {pnl_pct:+.2f}%")
        if holding_str:
            outcome.append(f"after {holding_str}")

        sentence = lead + (f" — {' '.join(outcome)}" if outcome else "")
        sentence = sentence.rstrip(".") + "."

        # Append the real strategy reason behind the exit, if one was
        # supplied and it isn't just a repeat of exit_reason/placeholder.
        clean = [
            r for r in (reasons or [])
            if r and r not in (exit_reason, "Paper trade executed")
        ]
        if clean:
            sentence += f" {clean[0].rstrip('.')}."

        return sentence

    # Buy / open insight — lead with the concrete signal, then trend.
    parts: list[str] = []

    clean = [r for r in (reasons or []) if r and r != "Paper trade executed"]
    if clean:
        parts.append(clean[0].rstrip("."))

    if trend and len(parts) < 2:
        trend_lower = trend.lower()
        if "up" in trend_lower or "bull" in trend_lower:
            parts.append("trend is bullish")
        elif "down" in trend_lower or "bear" in trend_lower:
            parts.append("despite bearish pressure")
        else:
            parts.append(f"trend: {trend}")

    if not parts:
        parts.append("Signal analysis complete")

    text = ". ".join(
        p[0].upper() + p[1:] if i == 0 else p for i, p in enumerate(parts[:2])
    )
    return text.rstrip(".") + "."


# ── Compact design language (headline-first, details secondary) ───────
#
# Used by the "at a glance" notifications and account commands.
# Rule: one bold headline with the result, then plain-language context,
# then (optionally) a monospace block for people who want the raw
# numbers. Never repeat the same number formatted two different ways.

def compact_header() -> str:
    """Minimal header — no separators, used by the compact message style."""
    return "🤖 *ZetBot AI*"


def detail_block(lines: list[str], label: str = "Details") -> str:
    """Render secondary/technical lines in a de-emphasized monospace block.

    Kept visually distinct from the headline so a non-technical reader
    can skip it, while it's still available for anyone who wants it.
    Telegram collapses multi-line monospace blocks visually smaller than
    the surrounding text, which is the effect we want for "secondary
    info" — a true tap-to-reveal spoiler needs MarkdownV2 (a much wider
    change across every message), so this is the safe equivalent.
    """
    clean = [ln for ln in lines if ln]
    if not clean:
        return ""
    return f"_{label}_\n```\n" + "\n".join(clean) + "\n```"


def note(text: str) -> str:
    """One-line, small/italic explainer — used instead of a jargon dump."""
    return f"_{text}_"


# ── Message assembly ──────────────────────────────────────────────────

def build_message(*blocks: str) -> str:
    """Join message blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


def empty_positions() -> str:
    """Standard 'no open positions' message."""
    return f"{header()}\n\nNo open positions."
