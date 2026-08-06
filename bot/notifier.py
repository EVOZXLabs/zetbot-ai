"""Centralized Notification Hub for ZetBot AI.

Single source of truth for all outbound Telegram notifications.
All trading modules must call this module — never call Telegram directly.

Usage::

    notifier = Notifier.from_config(app_config)
    notifier.notify_buy_opened(symbol="BTC/USDT", ...)
    notifier.notify_position_closed(symbol="BTC/USDT", ...)

Every notification is logged and never raises exceptions.
Telegram failure NEVER stops trading.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import timedelta
from typing import Any, Optional

import requests

logger = logging.getLogger("ZetBot")


class Notifier:
    """Centralized Telegram notification sender.

    Created once at startup, shared by all trading modules.
    All public methods are safe to call regardless of configuration —
    they silently no-op when disabled or misconfigured.
    API failures are logged and never propagated.
    """

    API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        enabled: bool = False,
        token: str = "",
        chat_id: str = "",
        timeout: int = 10,
        max_retry: int = 3,
        testing: bool = False,
        quote_currency: str = "USDT",
    ) -> None:
        self._enabled = enabled
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._max_retry = max_retry
        self._testing = testing
        self._quote_currency = quote_currency

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but token/chat_id missing — disabling",
            )
            self._enabled = False

        if self._enabled:
            logger.info(
                "Notifier enabled — timeout=%ds retry=%d",
                self._timeout, self._max_retry,
            )
        else:
            logger.debug("Notifier disabled")

    @classmethod
    def from_config(cls, config: Any) -> "Notifier":
        """Create Notifier from AppConfig or IConfigService."""
        return cls(
            enabled=bool(getattr(config, "telegram_enabled", False)),
            token=str(getattr(config, "telegram_token", "")),
            chat_id=str(getattr(config, "telegram_chat_id", "")),
            timeout=int(getattr(config, "telegram_timeout", 10)),
            max_retry=int(getattr(config, "telegram_retry", 3)),
            testing=bool(getattr(config, "testing", False)),
            quote_currency=str(getattr(config, "quote_currency", "USDT")),
        )

    @classmethod
    def from_env(cls) -> "Notifier":
        """Create Notifier from environment variables."""
        import os
        return cls(
            enabled=os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
            token=os.getenv("TELEGRAM_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            timeout=int(os.getenv("TELEGRAM_TIMEOUT", "10")),
            max_retry=int(os.getenv("TELEGRAM_RETRY", "3")),
            testing=os.getenv("TESTING", "false").lower() == "true",
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    #  Internal send
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Strip Telegram Markdown v1 markup to produce safe plain text.

        Removes paired ``*bold*``, ``_italic_``, `` `code` `` and
        `` ```block``` `` markers so the resulting string contains no
        Markdown syntax that could cause a parse rejection when
        ``parse_mode`` is absent.  Back-slash escapes added by
        ``md_escape`` are also unwound (``\\X`` → ``X``).

        Applied only when Telegram rejects the Markdown message — the
        plain-text fallback must not carry any residual markup.
        """
        import re
        # Unescape backslash-escaped specials (md_escape output) first
        # so they don't get double-processed by the regex passes.
        result = re.sub(r"\\(.)", r"\1", text)
        # Triple-backtick code blocks (``` ... ```) — strip fences + label
        result = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", result, flags=re.DOTALL)
        # Inline code (`...`)
        result = re.sub(r"`([^`]*)`", r"\1", result)
        # Bold (*...*)
        result = re.sub(r"\*([^*]+)\*", r"\1", result)
        # Italic (_..._)
        result = re.sub(r"_([^_]+)_", r"\1", result)
        # Links [text](url) — keep text, drop url
        result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)
        return result

    def _send(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send *text* to the configured Telegram chat.

        Retries on failure up to ``_max_retry`` times.
        Returns ``True`` on success, ``False`` otherwise.

        When Telegram rejects the message with a 400 "can't parse
        entities" error (malformed Markdown), the message is retried
        **once** with ``parse_mode`` removed AND with Markdown markup
        stripped from the text itself so the plain-text copy cannot
        trigger the same rejection.
        """
        if not self._enabled:
            logger.debug("[TG] Disabled — skipping send")
            return False

        if self._testing:
            logger.debug("[TG] Testing mode — send suppressed")
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
                logger.info("[TG] Sending notification (attempt %d/%d)", attempt, self._max_retry)
                resp = requests.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                logger.info("[TG] Sent successfully")
                return True
            except requests.RequestException as exc:
                # Telegram rejects Markdown text containing unescaped
                # special characters (e.g. from AI insight lines or error
                # messages) with a 400 "can't parse entities" error.
                # Retry with parse_mode entirely removed AND with all
                # Markdown markup stripped from the text — the plain-text
                # copy must be free of Markdown syntax characters so a
                # second rejection cannot occur.
                resp_obj = getattr(exc, "response", None)
                if (
                    parse_mode
                    and resp_obj is not None
                    and getattr(resp_obj, "status_code", None) == 400
                    and "parse" in (getattr(resp_obj, "text", "") or "").lower()
                ):
                    logger.warning("[TG] Markdown rejected — retrying as plain text (parse_mode disabled)")
                    payload.pop("parse_mode", None)
                    payload["text"] = self._strip_markdown(payload["text"])
                    parse_mode = ""
                    # Retry immediately without counting as a failed attempt
                    # so the user sees the message despite the Markdown error.
                    try:
                        resp2 = requests.post(url, json=payload, timeout=self._timeout)
                        resp2.raise_for_status()
                        logger.info("[TG] Sent successfully as plain text")
                        return True
                    except requests.RequestException as exc2:
                        logger.warning("[TG] Plain-text retry also failed: %s", exc2)
                logger.warning(
                    "[TG] Failed (attempt %d/%d): %s",
                    attempt, self._max_retry, exc,
                )
                if attempt < self._max_retry:
                    delay = min(10, 2 ** attempt) + random.uniform(0, 1)
                    import threading
                    threading.Event().wait(delay)

        logger.error("[TG] Send failed after %d attempts", self._max_retry)
        return False

    # ------------------------------------------------------------------
    #  Trading event notifications
    # ------------------------------------------------------------------

    def notify_buy_opened(
        self,
        symbol: str,
        exchange: str = "",
        timeframe: str = "",
        entry_price: float = 0.0,
        quantity: float = 0.0,
        position_size: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        take_profit_2: float = 0.0,
        take_profit_3: float = 0.0,
        strategy: str = "",
        reasons: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> bool:
        """Send BUY OPENED notification.

        Returns True on success, False otherwise.
        """
        logger.info("[TG] Sending notification: BUY_OPENED %s", symbol)
        try:
            from telegram.ui import (
                compact_header, wib_now, ai_insight,
                detail_block, build_message,
            )
            from telegram.formatter import fmt_price as fp, md_escape

            sl_pct = ((stop_loss - entry_price) / entry_price * 100) if entry_price > 0 else 0.0

            lines = [
                f"Stop Loss    {fp(stop_loss)}  ({sl_pct:+.2f}%)",
            ]

            if take_profit > 0:
                tp1_pct = ((take_profit - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
                lines.append(f"TP①          {fp(take_profit)}  ({tp1_pct:+.2f}%)")
            if take_profit_2 > 0:
                tp2_pct = ((take_profit_2 - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
                lines.append(f"TP②          {fp(take_profit_2)}  ({tp2_pct:+.2f}%)")
            if take_profit_3 > 0:
                tp3_pct = ((take_profit_3 - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
                lines.append(f"TP③          {fp(take_profit_3)}  ({tp3_pct:+.2f}%)")

            reasons = reasons or ["Pipeline execution"]
            where = " · ".join(p for p in (exchange, timeframe) if p)

            text = build_message(
                compact_header(),
                f"🟢 *BUY OPENED — {md_escape(symbol)}*"
                + (f"\n{md_escape(where)}" if where else ""),
                f"Entry {fp(entry_price)}\n🧠 {md_escape(ai_insight(reasons=reasons, is_buy=True))}",
                detail_block(lines, label="Trade plan"),
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send BUY notification: %s", exc)
            return False

    def notify_position_closed(
        self,
        symbol: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        pnl: float = 0.0,
        pnl_pct: float = 0.0,
        balance: float = 0.0,
        exit_reason: str = "Strategy Exit",
        holding_time: Optional[timedelta] = None,
        **kwargs: Any,
    ) -> bool:
        """Send POSITION CLOSED notification.

        Returns True on success, False otherwise.
        """
        quote = symbol.split("/")[1] if symbol and "/" in symbol else self._quote_currency
        logger.info("[TG] Sending notification: POSITION_CLOSED %s (PnL: %.2f %s)", symbol, pnl, quote)
        try:
            from telegram.ui import (
                compact_header, wib_now, pnl_emoji,
                ai_insight, detail_block, build_message,
            )
            from telegram.formatter import fmt_price as fp, fmt_holding, md_escape

            if holding_time is None:
                holding_time = timedelta()

            result_emoji = pnl_emoji(pnl)
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

            title = f"{symbol}" if symbol else "Position"
            pnl_label = "Profit" if pnl >= 0 else "Loss"

            text = build_message(
                compact_header(),
                f"{result_emoji} *POSITION CLOSED — {md_escape(title)}*\n"
                f"{pnl_label} {abs(pnl):,.2f} {quote} ({roi_pct:+.2f}%) · Held {holding_str}",
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
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send close notification: %s", exc)
            return False

    def notify_take_profit(
        self,
        symbol: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        profit: float = 0.0,
        holding_time: Optional[timedelta] = None,
        **kwargs: Any,
    ) -> bool:
        """Send TAKE PROFIT HIT notification."""
        logger.info("[TG] Sending notification: TAKE_PROFIT %s", symbol)
        try:
            from telegram.ui import compact_header, wib_now, detail_block, build_message
            from telegram.formatter import fmt_price as fp, fmt_holding, md_escape

            quote = symbol.split("/")[1] if symbol and "/" in symbol else "USDT"
            if holding_time is None:
                holding_time = timedelta()
            holding_str = fmt_holding(holding_time.total_seconds())

            text = build_message(
                compact_header(),
                f"🎯 *TAKE PROFIT HIT — {md_escape(symbol)}*\n"
                f"Profit {profit:+,.2f} {quote} · Held {holding_str}",
                detail_block(
                    [f"Entry  {fp(entry_price)}", f"Exit   {fp(exit_price)}"],
                    label="Trade details",
                ),
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send TP notification: %s", exc)
            return False

    def notify_stop_loss(
        self,
        symbol: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        loss: float = 0.0,
        holding_time: Optional[timedelta] = None,
        **kwargs: Any,
    ) -> bool:
        """Send STOP LOSS HIT notification."""
        logger.info("[TG] Sending notification: STOP_LOSS %s", symbol)
        try:
            from telegram.ui import compact_header, wib_now, detail_block, build_message
            from telegram.formatter import fmt_price as fp, fmt_holding, md_escape

            quote = symbol.split("/")[1] if symbol and "/" in symbol else "USDT"
            if holding_time is None:
                holding_time = timedelta()
            holding_str = fmt_holding(holding_time.total_seconds())

            text = build_message(
                compact_header(),
                f"🛑 *STOP LOSS HIT — {md_escape(symbol)}*\n"
                f"Loss {loss:+,.2f} {quote} · Held {holding_str}",
                detail_block(
                    [f"Entry  {fp(entry_price)}", f"Exit   {fp(exit_price)}"],
                    label="Trade details",
                ),
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send SL notification: %s", exc)
            return False

    def notify_trade_rejected(
        self,
        symbol: str,
        reason: str = "",
        **kwargs: Any,
    ) -> bool:
        """Send TRADE REJECTED notification (low priority system event)."""
        logger.info("[TG] Sending notification: TRADE_REJECTED %s — %s", symbol, reason)
        try:
            from telegram.ui import compact_header, wib_now, build_message
            from telegram.formatter import md_escape

            text = build_message(
                compact_header(),
                f"⚠️ *TRADE REJECTED — {md_escape(symbol)}*\n{md_escape(reason)}",
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send rejection notification: %s", exc)
            return False

    # ------------------------------------------------------------------
    #  System event notifications
    # ------------------------------------------------------------------

    def notify_bot_started(
        self,
        symbol: str = "",
        timeframe: str = "",
        exchange: str = "",
        balance: float = 0.0,
        equity: float = 0.0,
    ) -> bool:
        """Send BOT STARTED notification."""
        logger.info("[TG] Sending notification: BOT_STARTED")
        try:
            from telegram.ui import compact_header, wib_now, build_message
            from telegram.formatter import md_escape

            where = " • ".join(p for p in (symbol, exchange, timeframe) if p)
            bal_line = f"\nTotal {equity:,.2f} {self._quote_currency} · Cash {balance:,.2f} {self._quote_currency}" if equity > 0 else ""
            text = build_message(
                compact_header(),
                "🟢 *BOT STARTED*" + (f"\n{md_escape(where)}" if where else "") + bal_line,
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send bot_started: %s", exc)
            return False

    def notify_bot_stopped(
        self,
        cycles: int = 0,
        balance: float = 0.0,
        equity: float = 0.0,
    ) -> bool:
        """Send BOT STOPPED notification."""
        logger.info("[TG] Sending notification: BOT_STOPPED")
        try:
            from telegram.ui import compact_header, build_message

            equity_line = f" · Total {equity:,.2f} {self._quote_currency}" if equity > 0 else ""
            text = build_message(
                compact_header(),
                f"🔴 *BOT STOPPED*\n"
                f"{cycles} cycles run · Cash {balance:,.2f} {self._quote_currency}{equity_line}",
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send bot_stopped: %s", exc)
            return False

    def notify_error(self, message: str) -> bool:
        """Send ERROR notification."""
        logger.info("[TG] Sending notification: ERROR — %s", message[:80])
        try:
            from telegram.ui import compact_header, wib_now, build_message
            from telegram.formatter import md_escape

            text = build_message(
                compact_header(),
                f"⚠️ *ERROR*\n`{md_escape(message)}`",
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send error notification: %s", exc)
            return False

    def notify_system(self, message: str) -> bool:
        """Send generic system notification."""
        logger.info("[TG] Sending notification: SYSTEM")
        try:
            from telegram.ui import compact_header, wib_now, build_message
            from telegram.formatter import md_escape

            text = build_message(
                compact_header(),
                f"ℹ️ *SYSTEM*\n{md_escape(message)}",
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send system notification: %s", exc)
            return False

    def notify_daily_summary(self, stats: dict, balance: float) -> bool:
        """Send daily trading summary."""
        logger.info("[TG] Sending notification: DAILY_SUMMARY")
        try:
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
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send daily summary: %s", exc)
            return False

    def notify_state_restored(
        self,
        balance: float = 0.0,
        has_position: bool = False,
        trades: int = 0,
    ) -> bool:
        """Send STATE RESTORED notification."""
        logger.info("[TG] Sending notification: STATE_RESTORED")
        try:
            from telegram.ui import compact_header, wib_now, build_message
            pos_str = "YES — managing open position" if has_position else "NO"
            text = build_message(
                compact_header(),
                f"♻️ *STATE RESTORED*\n"
                f"Balance {balance:,.2f} {self._quote_currency} · Open position: {pos_str}\n"
                f"Past trades: {trades}",
                wib_now().replace("\n", ", "),
            )
            return self._send(text)
        except Exception as exc:
            logger.warning("[TG] Failed to send state_restored: %s", exc)
            return False

    # ------------------------------------------------------------------
    #  Raw send (for custom formatting)
    # ------------------------------------------------------------------

    def send(self, message: str) -> bool:
        """Send a raw message. Returns True on success."""
        return self._send(message)

    # ------------------------------------------------------------------
    #  Backward-compatible aliases (match TelegramNotifier interface)
    # ------------------------------------------------------------------

    def bot_started(self, symbol: str = "", timeframe: str = "",
                    exchange: str = "") -> None:
        """Backward-compatible alias for notify_bot_started."""
        self.notify_bot_started(symbol, timeframe, exchange)

    def bot_stopped(self, cycles: int = 0, balance: float = 0.0) -> None:
        """Backward-compatible alias for notify_bot_stopped."""
        self.notify_bot_stopped(cycles, balance)

    def buy_opened(self, symbol: str = "", timeframe: str = "",
                   exchange: str = "", entry_price: float = 0.0,
                   quantity: float = 0.0, position_size: float = 0.0,
                   stop_loss: float = 0.0, take_profit: float = 0.0,
                   reasons: Optional[list[str]] = None) -> None:
        """Backward-compatible alias for notify_buy_opened."""
        self.notify_buy_opened(
            symbol=symbol, timeframe=timeframe, exchange=exchange,
            entry_price=entry_price, quantity=quantity,
            position_size=position_size, stop_loss=stop_loss,
            take_profit=take_profit, reasons=reasons,
        )

    def trade_closed(self, exit_price: float = 0.0, pnl_usd: float = 0.0,
                     pnl_pct: float = 0.0, balance: float = 0.0,
                     exit_reason: str = "", holding_time: Optional[timedelta] = None,
                     symbol: str = "", entry_price: float = 0.0) -> None:
        """Backward-compatible alias for notify_position_closed."""
        self.notify_position_closed(
            symbol=symbol, entry_price=entry_price, exit_price=exit_price,
            pnl=pnl_usd, pnl_pct=pnl_pct, balance=balance,
            exit_reason=exit_reason, holding_time=holding_time,
        )

    def state_restored(self, balance: float = 0.0,
                       has_position: bool = False, trades: int = 0) -> None:
        """Backward-compatible alias for notify_state_restored."""
        self.notify_state_restored(balance, has_position, trades)

    def error_occurred(self, message: str = "") -> None:
        """Backward-compatible alias for notify_error."""
        self.notify_error(message)

    def daily_summary(self, stats: dict = None, balance: float = 0.0) -> None:
        """Backward-compatible alias for notify_daily_summary."""
        self.notify_daily_summary(stats or {}, balance)
