"""
Telegram Notifier Module

Sends trading notifications via the Telegram Bot API.

ZetBot AI
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from bot.config import CONFIG

logger = logging.getLogger("ZetBot")

# requests exceptions embed the full request URL — including the bot
# token — into str(exc); logging it verbatim would leak the token.
_BOT_TOKEN_IN_URL = re.compile(r"bot\d{5,}:[A-Za-z0-9_\-]{20,}")


def _redact(msg: Any) -> str:
    return _BOT_TOKEN_IN_URL.sub("bot<REDACTED>", str(msg))


class TelegramNotifier:
    """Send formatted trading notifications through Telegram.

    All public methods are safe to call regardless of configuration —
    they silently no-op when the notifier is disabled or misconfigured.
    API failures are logged and never propagated.
    """

    API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._enabled: bool = bool(CONFIG.get("telegram_enabled", False))
        self._token: str = str(CONFIG.get("telegram_token", ""))
        self._chat_id: str = str(CONFIG.get("telegram_chat_id", ""))
        self._timeout: int = int(CONFIG.get("telegram_timeout", 10))
        self._max_retry: int = int(CONFIG.get("telegram_retry", 3))
        self._quote_currency: str = str(CONFIG.get("quote_currency", "USDT"))

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but token/chat_id missing — disabling",
            )
            self._enabled = False

        if self._enabled:
            logger.info(
                "Telegram notifier enabled — timeout=%ds retry=%d",
                self._timeout, self._max_retry,
            )
        else:
            logger.debug("Telegram notifier disabled")

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _send(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send *text* to the configured Telegram chat.

        Retries on failure up to ``_max_retry`` times.
        Returns ``True`` on success, ``False`` otherwise.

        When ``TESTING=true`` the method returns ``True`` without making
        any HTTP request so that unit tests never hit the live API.
        """
        if not self._enabled:
            return False

        if bool(CONFIG.get("testing", False)):
            logger.debug("Testing mode — Telegram send suppressed")
            return True

        url = self.API_BASE.format(token=self._token)
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        for attempt in range(1, self._max_retry + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                return True
            except requests.RequestException as exc:
                # Telegram rejects Markdown text with unescaped special
                # characters (400 "can't parse entities").  Resend the
                # same text without parse_mode so the message always
                # arrives instead of being lost.
                resp = getattr(exc, "response", None)
                if (
                    parse_mode
                    and resp is not None
                    and resp.status_code == 400
                    and "parse" in (getattr(resp, "text", "") or "").lower()
                ):
                    logger.warning("Markdown rejected — resending as plain text")
                    payload.pop("parse_mode", None)
                    parse_mode = ""
                logger.warning(
                    "Telegram send attempt %d/%d failed: %s",
                    attempt, self._max_retry, _redact(exc),
                )
                if attempt < self._max_retry:
                    import random
                    import time
                    delay = min(30, 2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)

        logger.error("Telegram send failed after %d attempts", self._max_retry)
        return False

    # ------------------------------------------------------------------
    #  Public notification methods
    # ------------------------------------------------------------------

    def send(self, message: str) -> bool:
        """Send a raw message. Returns True on success."""
        return self._send(message)

    def bot_started(
        self,
        symbol: str,
        timeframe: str,
        exchange: str,
    ) -> None:
        """Notify that the trading bot has started."""
        from telegram.ui import compact_header, wib_now, build_message
        from telegram.formatter import md_escape
        where = " • ".join(p for p in (symbol, exchange, timeframe) if p)
        text = build_message(
            compact_header(),
            "🟢 *BOT STARTED*" + (f"\n{md_escape(where)}" if where else ""),
            wib_now().replace("\n", ", "),
        )
        self._send(text)

    def bot_stopped(
        self,
        cycles: int = 0,
        balance: float = 0.0,
        equity: float = 0.0,
    ) -> None:
        """Notify that the trading bot has stopped."""
        from telegram.ui import compact_header, build_message
        text = build_message(
            compact_header(),
            f"🔴 *BOT STOPPED*\n"
            f"{cycles} cycles run · Balance {balance:,.2f} {self._quote_currency}",
        )
        self._send(text)

    def buy_opened(
        self,
        symbol: str,
        timeframe: str,
        exchange: str,
        entry_price: float,
        quantity: float,
        position_size: float,
        stop_loss: float,
        take_profit: float,
        reasons: list[str],
    ) -> None:
        """Notify that a BUY position was opened."""
        from telegram.ui import (
            compact_header, wib_now, ai_insight,
            detail_block, build_message,
        )
        from telegram.formatter import fmt_price as fp, md_escape

        sl_pct = ((stop_loss - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
        tp_pct = ((take_profit - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
        where = " · ".join(p for p in (exchange, timeframe) if p)

        text = build_message(
            compact_header(),
            f"🟢 *BUY OPENED — {md_escape(symbol)}*" + (f"\n{md_escape(where)}" if where else ""),
            f"Entry {fp(entry_price)}\n🧠 {md_escape(ai_insight(reasons=reasons, is_buy=True))}",
            detail_block(
                [
                    f"Stop Loss    {fp(stop_loss)}  ({sl_pct:+.2f}%)",
                    f"Take Profit  {fp(take_profit)}  ({tp_pct:+.2f}%)",
                ],
                label="Trade plan",
            ),
            wib_now().replace("\n", ", "),
        )
        self._send(text)

    def trade_closed(
        self,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        balance: float,
        exit_reason: str,
        holding_time: timedelta,
        symbol: str = "",
        entry_price: float = 0.0,
    ) -> None:
        """Notify that a trade was closed.

        One message, one focus: the result comes first, one plain-language
        line for why, then Entry/Exit as secondary technical info for
        anyone who wants to double-check the numbers.
        """
        from telegram.ui import (
            compact_header, wib_now, pnl_emoji,
            ai_insight, detail_block, build_message,
        )
        from telegram.formatter import fmt_price as fp, fmt_holding, md_escape

        holding_str = fmt_holding(holding_time.total_seconds())

        roi_pct = (
            ((exit_price - entry_price) / entry_price * 100)
            if entry_price > 0 and exit_price > 0 else 0.0
        )
        if roi_pct == 0.0:
            roi_pct = pnl_pct

        insight = ai_insight(
            reasons=[exit_reason],
            is_buy=False,
            exit_reason=exit_reason,
            pnl_pct=roi_pct,
            holding_str=holding_str,
        )
        title = symbol or "Position"

        quote = symbol.split("/")[1] if symbol and "/" in symbol else "USDT"
        text = build_message(
            compact_header(),
            f"{pnl_emoji(pnl_usd)} *POSITION CLOSED — {md_escape(title)}*\n"
            f"Profit {pnl_usd:+,.2f} {quote} ({roi_pct:+.2f}%) · Held {holding_str}",
            f"🧠 AI Insight: {md_escape(insight)}\nBalance now {balance:,.2f} {quote}",
            detail_block(
                [
                    f"Entry  {fp(entry_price)}",
                    f"Exit   {fp(exit_price)}",
                ],
                label="Trade details",
            ),
            wib_now().replace("\n", ", "),
        )
        self._send(text)

    def state_restored(
        self,
        balance: float,
        has_position: bool,
        trades: int,
    ) -> None:
        """Notify that persistent state was restored."""
        from telegram.ui import compact_header, wib_now, build_message
        pos_str = "YES — managing open position" if has_position else "NO"
        text = build_message(
            compact_header(),
            f"♻️ *STATE RESTORED*\n"
            f"Balance {balance:,.2f} {self._quote_currency} · Open position: {pos_str}\n"
            f"Past trades: {trades}",
            wib_now().replace("\n", ", "),
        )
        self._send(text)

    def error_occurred(
        self,
        message: str,
    ) -> None:
        """Notify that an error occurred."""
        from telegram.ui import compact_header, wib_now, build_message
        from telegram.formatter import md_escape
        text = build_message(
            compact_header(),
            f"⚠️ *ERROR*\n`{md_escape(message)}`",
            wib_now().replace("\n", ", "),
        )
        self._send(text)

    def daily_summary(self, stats: dict, balance: float) -> None:
        """Send a daily trading summary."""
        from telegram.ui import (
            compact_header, wib_now, confidence_bar, pnl_emoji, build_message,
        )
        from telegram.formatter import fmt_pf

        if stats.get("total_trades", 0) == 0:
            text = build_message(
                compact_header(),
                "📅 *DAILY SUMMARY*\nNo trades completed today.",
                f"Balance {balance:,.2f} {self._quote_currency}",
                wib_now().replace("\n", ", "),
            )
        else:
            win_count = stats.get("win_count", 0)
            loss_count = stats.get("loss_count", 0)
            total = win_count + loss_count
            win_rate = stats.get("win_rate", 0.0)
            total_pnl = stats.get("total_profit", 0.0)
            pf = stats.get("profit_factor", 0.0)

            text = build_message(
                compact_header(),
                f"📅 *DAILY SUMMARY* — {total} trades\n"
                f"{pnl_emoji(total_pnl)} PnL {total_pnl:+,.2f} {self._quote_currency} · "
                f"{win_count}W/{loss_count}L",
                f"Win rate {confidence_bar(win_rate)}\n"
                f"Balance {balance:,.2f} {self._quote_currency} · Profit factor {fmt_pf(pf)}",
                wib_now().replace("\n", ", "),
            )
        self._send(text)
