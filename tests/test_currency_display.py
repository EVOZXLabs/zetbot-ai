"""Tests for dynamic quote currency display in Telegram notifications.

Verifies that system notifications (BOT_STARTED, BOT_STOPPED, etc.)
display the configured quote currency (USDT for Binance, IDR for Indodax)
instead of a hardcoded currency symbol.
"""

from unittest.mock import MagicMock, patch

import pytest

from bot.notifier import Notifier


def _make_notifier(quote_currency: str = "USDT", **kwargs) -> Notifier:
    defaults = {
        "enabled": True,
        "token": "123:ABC",
        "chat_id": "456",
        "timeout": 10,
        "max_retry": 3,
        "testing": False,
    }
    defaults.update(kwargs)
    return Notifier(quote_currency=quote_currency, **defaults)


class TestNotifierCurrencyDisplay:

    # ------------------------------------------------------------------
    #  BOT_STARTED
    # ------------------------------------------------------------------

    def test_bot_started_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_started(balance=10_000.0, equity=10_500.0)
        text = mock_send.call_args[0][0]
        assert "USDT" in text
        assert "10,000" in text
        assert "10,500" in text

    def test_bot_started_idr(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_started(balance=150_000_000.0, equity=155_000_000.0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "150,000,000" in text
        assert "155,000,000" in text

    def test_bot_started_no_hardcoded_usdt(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_started(balance=100_000.0, equity=100_000.0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "USDT" not in text

    # ------------------------------------------------------------------
    #  BOT_STOPPED
    # ------------------------------------------------------------------

    def test_bot_stopped_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_stopped(cycles=10, balance=10_500.0, equity=10_500.0)
        text = mock_send.call_args[0][0]
        assert "USDT" in text
        assert "10" in text

    def test_bot_stopped_idr(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_stopped(cycles=5, balance=200_000_000.0, equity=210_000_000.0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "USDT" not in text

    # ------------------------------------------------------------------
    #  DAILY SUMMARY
    # ------------------------------------------------------------------

    def test_daily_summary_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        stats = {"total_trades": 5, "win_count": 3, "loss_count": 2,
                 "win_rate": 60.0, "total_profit": 150.0, "profit_factor": 2.0}
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=10_150.0)
        text = mock_send.call_args[0][0]
        assert "USDT" in text

    def test_daily_summary_idr(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        stats = {"total_trades": 3, "win_count": 2, "loss_count": 1,
                 "win_rate": 66.67, "total_profit": 500_000.0, "profit_factor": 1.5}
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=100_500_000.0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "USDT" not in text

    def test_daily_summary_no_trades_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        stats = {"total_trades": 0}
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=10_000.0)
        text = mock_send.call_args[0][0]
        assert "USDT" in text

    def test_daily_summary_no_trades_idr(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        stats = {"total_trades": 0}
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=100_000_000.0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "USDT" not in text

    # ------------------------------------------------------------------
    #  STATE RESTORED
    # ------------------------------------------------------------------

    def test_state_restored_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        with patch.object(n, "_send") as mock_send:
            n.notify_state_restored(balance=10_000.0, has_position=True, trades=3)
        text = mock_send.call_args[0][0]
        assert "USDT" in text

    def test_state_restored_idr(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_state_restored(balance=100_000_000.0, has_position=False, trades=0)
        text = mock_send.call_args[0][0]
        assert "IDR" in text
        assert "USDT" not in text

    # ------------------------------------------------------------------
    #  from_config passes quote_currency
    # ------------------------------------------------------------------

    def test_from_config_quote_currency(self) -> None:
        cfg = MagicMock()
        cfg.telegram_enabled = True
        cfg.telegram_token = "tok"
        cfg.telegram_chat_id = "cid"
        cfg.telegram_timeout = 5
        cfg.telegram_retry = 2
        cfg.testing = False
        cfg.quote_currency = "IDR"
        n = Notifier.from_config(cfg)
        assert n._quote_currency == "IDR"

    def test_from_config_default_quote_currency(self) -> None:
        cfg = MagicMock(spec=[])
        cfg.telegram_enabled = True
        cfg.telegram_token = "tok"
        cfg.telegram_chat_id = "cid"
        n = Notifier.from_config(cfg)
        assert n._quote_currency == "USDT"

    # ------------------------------------------------------------------
    #  Per-trade notifications still use symbol-based extraction
    # ------------------------------------------------------------------

    def test_position_closed_uses_symbol_quote(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="BTC/USDT", pnl=100.0, balance=10_100.0,
                entry_price=50_000.0, exit_price=51_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "USDT" in text  # extracted from symbol BTC/USDT, not from config IDR

    def test_take_profit_uses_symbol_quote(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_take_profit(
                symbol="ETH/USDT", profit=50.0,
                entry_price=3_000.0, exit_price=3_100.0,
            )
        text = mock_send.call_args[0][0]
        assert "USDT" in text  # from symbol, not config

    def test_stop_loss_uses_symbol_quote(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_stop_loss(
                symbol="SOL/USDT", loss=-30.0,
                entry_price=150.0, exit_price=145.0,
            )
        text = mock_send.call_args[0][0]
        assert "USDT" in text  # from symbol, not config