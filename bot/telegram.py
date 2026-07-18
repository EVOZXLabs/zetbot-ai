"""
Telegram Notifier Module

Sends trading notifications via the Telegram Bot API.

ZetBot AI
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from bot.config import CONFIG

logger = logging.getLogger("ZetBot")


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
                logger.warning(
                    "Telegram send attempt %d/%d failed: %s",
                    attempt, self._max_retry, exc,
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
        from telegram.ui import header, SEPARATOR, wib_now
        text = (
            f"{header()}\n\n"
            f"🟢 *BOT STARTED*\n"
            f"{SEPARATOR}\n"
            f"📊 {symbol} • {exchange} • {timeframe}\n"
            f"{SEPARATOR}\n"
            f"🕐 {wib_now()}"
        )
        self._send(text)

    def bot_stopped(
        self,
        cycles: int,
        balance: float,
    ) -> None:
        """Notify that the trading bot has stopped."""
        from telegram.ui import header, SEPARATOR
        text = (
            f"{header()}\n\n"
            f"🔴 *BOT STOPPED*\n"
            f"{SEPARATOR}\n"
            f"📊 Cycles: {cycles}\n"
            f"💰 Balance: ${balance:,.2f}"
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
            header, SEPARATOR, wib_now, progress_bar,
            ai_insight, build_message,
        )
        from telegram.formatter import fmt_price as fp

        sl_pct = ((stop_loss - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
        tp_pct = ((take_profit - entry_price) / entry_price * 100) if entry_price > 0 else 0.0

        text = build_message(
            header(),
            f"🟢 *BUY OPENED*\n{symbol} • {exchange} • {timeframe}",
            f"{SEPARATOR}\n💰 Entry\n{fp(entry_price)}\n\n"
            f"📍 Current\n{fp(entry_price)}",
            f"{SEPARATOR}\n🛑 Stop Loss\n{sl_pct:+.2f}%\n\n"
            f"🎯 Take Profit\n{tp_pct:+.2f}%",
            f"{SEPARATOR}\n🧠 *AI Insight*\n"
            f"{ai_insight(reasons=reasons, is_buy=True)}",
            f"{SEPARATOR}\n🕐 {wib_now()}",
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
        """Notify that a trade was closed."""
        from telegram.ui import (
            header, SEPARATOR, wib_now,
            pnl_emoji, ai_insight, build_message,
        )
        from telegram.formatter import fmt_price as fp, fmt_holding

        emoji_map = {
            "Take Profit": "🟢",
            "Stop Loss": "🔴",
            "Strategy Exit": "⚪",
        }
        reason_emoji = emoji_map.get(exit_reason, "❓")
        holding_str = fmt_holding(holding_time.total_seconds())

        roi_pct = (
            ((exit_price - entry_price) / entry_price * 100)
            if entry_price > 0 and exit_price > 0 else 0.0
        )
        if roi_pct == 0.0:
            roi_pct = pnl_pct

        blocks = [
            header(),
            f"🔴 *POSITION CLOSED*\n{symbol}" if symbol else f"🔴 *POSITION CLOSED*",
            f"{SEPARATOR}\n"
            f"💰 Entry\n{fp(entry_price)}\n\n"
            f"🚪 Exit\n{fp(exit_price)}",
            f"{SEPARATOR}\n"
            f"📈 Profit\n{pnl_emoji(pnl_usd)} ${pnl_usd:+,.2f}\n\n"
            f"🕒 Held\n{holding_str}",
        ]

        insight = ai_insight(
            reasons=[exit_reason],
            is_buy=False,
        )
        blocks.append(f"{SEPARATOR}\n🧠 *AI Insight*\n{insight}")
        blocks.append(f"{SEPARATOR}\n💹 Balance\n${balance:,.2f}")

        text = build_message(*blocks)
        self._send(text)

    def state_restored(
        self,
        balance: float,
        has_position: bool,
        trades: int,
    ) -> None:
        """Notify that persistent state was restored."""
        from telegram.ui import header, SEPARATOR, wib_now, build_message
        pos_str = "YES — managing open position" if has_position else "NO"
        text = build_message(
            header(),
            f"♻️ *STATE RESTORED*\n{SEPARATOR}",
            f"💰 Balance: ${balance:,.2f}\n"
            f"📂 Open Position: {pos_str}\n"
            f"📊 Past Trades: {trades}",
            f"🕐 {wib_now()}",
        )
        self._send(text)

    def error_occurred(
        self,
        message: str,
    ) -> None:
        """Notify that an error occurred."""
        from telegram.ui import header, SEPARATOR, wib_now, build_message
        text = build_message(
            header(),
            f"⚠️ *ERROR*\n{SEPARATOR}\n`{message}`",
            f"🕐 {wib_now()}",
        )
        self._send(text)

    def daily_summary(self, stats: dict, balance: float) -> None:
        """Send a daily trading summary."""
        from telegram.ui import (
            header, SEPARATOR, wib_now, confidence_bar, build_message,
        )

        if stats.get("total_trades", 0) == 0:
            text = build_message(
                header(),
                f"📅 *DAILY SUMMARY*\n{SEPARATOR}",
                f"No trades completed today.\n💰 Balance: ${balance:,.2f}",
                f"🕐 {wib_now()}",
            )
        else:
            win_count = stats.get("win_count", 0)
            loss_count = stats.get("loss_count", 0)
            total = win_count + loss_count
            win_rate = stats.get("win_rate", 0.0)
            total_pnl = stats.get("total_profit", 0.0)
            pf = stats.get("profit_factor", 0.0)

            text = build_message(
                header(),
                f"📅 *DAILY SUMMARY*\n{SEPARATOR}",
                f"📊 Trades: {total}\n"
                f"✅ Wins: {win_count}  ❌ Losses: {loss_count}\n"
                f"📈 Win Rate: {confidence_bar(win_rate)}",
                f"💰 PnL: ${total_pnl:+,.2f}\n"
                f"📐 Profit Factor: {pf:.2f}\n"
                f"💹 Balance: ${balance:,.2f}",
                f"🕐 {wib_now()}",
            )
        self._send(text)
