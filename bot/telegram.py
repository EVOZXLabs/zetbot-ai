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

    @staticmethod
    def _format_timedelta(td: timedelta) -> str:
        total_sec = int(td.total_seconds())
        hours, remainder = divmod(total_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    #  Public notification methods
    # ------------------------------------------------------------------

    def bot_started(
        self,
        symbol: str,
        timeframe: str,
        exchange: str,
    ) -> None:
        """Notify that the trading bot has started."""
        text = (
            f"\U0001f916 *Bot Started*\n"
            f"Exchange: `{exchange}`\n"
            f"Symbol: `{symbol}`\n"
            f"Timeframe: `{timeframe}`"
        )
        self._send(text)

    def bot_stopped(
        self,
        cycles: int,
        balance: float,
    ) -> None:
        """Notify that the trading bot has stopped."""
        text = (
            f"\U0001f6d1 *Bot Stopped*\n"
            f"Cycles: `{cycles}`\n"
            f"Final Balance: `{balance:.2f}` USDT"
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
        text = (
            f"\U0001f7e2 *BUY OPENED*\n"
            f"Exchange: `{exchange}`\n"
            f"Symbol: `{symbol}`\n"
            f"Timeframe: `{timeframe}`\n"
            f"Entry: `{entry_price:.2f}`\n"
            f"Qty: `{quantity:.6f}`\n"
            f"Position Size: `{position_size:.2f}` USDT\n"
            f"Stop Loss: `{stop_loss:.2f}`\n"
            f"Take Profit: `{take_profit:.2f}`\n"
            f"Reasons: `{ ' | '.join(reasons) }`\n"
            f"UTC: `{self._utc_now()}`"
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
    ) -> None:
        """Notify that a trade was closed."""
        reason_emoji = {
            "Take Profit": "\U0001f7e2",
            "Stop Loss": "\U0001f534",
            "Strategy Exit": "\u26aa",
        }
        emoji = reason_emoji.get(exit_reason, "\u2753")
        result_tag = "WIN" if pnl_usd >= 0 else "LOSS"
        holding_str = self._format_timedelta(holding_time)
        text = (
            f"{emoji} *{exit_reason}*\n"
            f"Exit Price: `{exit_price:.2f}`\n"
            f"PnL: `{pnl_usd:+.2f}` USDT\n"
            f"PnL %: `{pnl_pct:+.2f}%`\n"
            f"Balance: `{balance:.2f}` USDT\n"
            f"Held: `{holding_str}`\n"
            f"Result: `{result_tag}`\n"
            f"UTC: `{self._utc_now()}`"
        )
        self._send(text)

    def state_restored(
        self,
        balance: float,
        has_position: bool,
        trades: int,
    ) -> None:
        """Notify that persistent state was restored."""
        text = (
            f"\u267b\ufe0f *State Restored*\n"
            f"Balance: `{balance:.2f}` USDT\n"
            f"Open Position: `{'YES' if has_position else 'NO'}`\n"
            f"Past Trades: `{trades}`"
        )
        self._send(text)

    def error_occurred(
        self,
        message: str,
    ) -> None:
        """Notify that an error occurred."""
        text = (
            f"\u26a0\ufe0f *Error*\n"
            f"`{message}`"
        )
        self._send(text)

    def daily_summary(self, stats: dict, balance: float) -> None:
        """Send a daily trading summary."""
        emoji = "\U0001f4c5"
        if stats.get("total_trades", 0) == 0:
            text = (
                f"{emoji} *Daily Summary*\n"
                f"No trades completed today.\n"
                f"Balance: `{balance:.2f}` USDT\n"
                f"UTC: `{self._utc_now()}`"
            )
        else:
            text = (
                f"{emoji} *Daily Summary*\n"
                f"Trades: `{stats['total_trades']}`\n"
                f"Wins / Losses: `{stats['win_count']}` / `{stats['loss_count']}`\n"
                f"Win Rate: `{stats['win_rate']:.1f}%`\n"
                f"Total PnL: `{stats['total_profit']:+.2f}` USDT\n"
                f"Profit Factor: `{stats['profit_factor']:.2f}`\n"
                f"Avg Win / Avg Loss: `{stats['average_win']:+.2f}` / `{stats['average_loss']:+.2f}` USDT\n"
                f"Balance: `{balance:.2f}` USDT\n"
                f"UTC: `{self._utc_now()}`"
            )
        self._send(text)
