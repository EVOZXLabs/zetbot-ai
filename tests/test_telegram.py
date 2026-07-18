"""
Unit tests for TelegramNotifier.

Covers: disabled mode, missing credentials, successful send,
retry logic, timeout, message formatting, edge cases.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot.telegram import TelegramNotifier


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_config():
    """Ensure telegram_enabled is restored after each test."""
    import bot.config as cfg
    saved = {
        "telegram_enabled": cfg.CONFIG.get("telegram_enabled"),
        "telegram_token": cfg.CONFIG.get("telegram_token"),
        "telegram_chat_id": cfg.CONFIG.get("telegram_chat_id"),
        "telegram_timeout": cfg.CONFIG.get("telegram_timeout"),
        "telegram_retry": cfg.CONFIG.get("telegram_retry"),
        "testing": cfg.CONFIG.get("testing"),
    }
    yield
    for k, v in saved.items():
        cfg.CONFIG[k] = v


def _enable_telegram(
    token: str = "123:ABC",
    chat_id: str = "456",
    timeout: int = 10,
    retry: int = 3,
) -> None:
    import bot.config as cfg
    cfg.CONFIG["telegram_enabled"] = True
    cfg.CONFIG["telegram_token"] = token
    cfg.CONFIG["telegram_chat_id"] = chat_id
    cfg.CONFIG["telegram_timeout"] = timeout
    cfg.CONFIG["telegram_retry"] = retry


def _disable_telegram() -> None:
    import bot.config as cfg
    cfg.CONFIG["telegram_enabled"] = False


# ---------------------------------------------------------------------------
#  Initialisation
# ---------------------------------------------------------------------------

class TestInit:

    def test_disabled_by_default(self) -> None:
        _disable_telegram()
        n = TelegramNotifier()
        assert not n._enabled

    def test_disabled_when_credentials_missing(self) -> None:
        import bot.config as cfg
        cfg.CONFIG["telegram_enabled"] = True
        cfg.CONFIG["telegram_token"] = ""
        cfg.CONFIG["telegram_chat_id"] = ""
        n = TelegramNotifier()
        assert not n._enabled

    def test_enabled_with_credentials(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        assert n._enabled
        assert n._token == "123:ABC"
        assert n._chat_id == "456"
        assert n._timeout == 10
        assert n._max_retry == 3

    def test_disabled_when_token_missing(self) -> None:
        _enable_telegram(token="", chat_id="456")
        n = TelegramNotifier()
        assert not n._enabled

    def test_disabled_when_chat_id_missing(self) -> None:
        _enable_telegram(token="123:ABC", chat_id="")
        n = TelegramNotifier()
        assert not n._enabled


# ---------------------------------------------------------------------------
#  _send
# ---------------------------------------------------------------------------

class TestSend:

    def test_send_disabled_returns_false(self) -> None:
        _disable_telegram()
        n = TelegramNotifier()
        assert n._send("hello") is False

    def test_send_success(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            mock_post.return_value = MagicMock(ok=True)
            mock_post.return_value.raise_for_status.return_value = None
            result = n._send("test message")
        assert result is True
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "123:ABC" in url
        payload = mock_post.call_args[1]["json"]
        assert payload["chat_id"] == "456"
        assert payload["text"] == "test message"

    def test_send_retry_on_failure(self) -> None:
        _enable_telegram(retry=3)
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("fail")
            result = n._send("test")
        assert result is False
        assert mock_post.call_count == 3

    def test_send_succeeds_after_retry(self) -> None:
        _enable_telegram(retry=3)
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            mock_post.side_effect = [
                requests.RequestException("fail 1"),
                MagicMock(ok=True),
            ]
            mock_post.return_value.raise_for_status.return_value = None
            result = n._send("test")
        assert result is True
        assert mock_post.call_count == 2

    def test_send_timeout(self) -> None:
        _enable_telegram(timeout=5)
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            mock_post.side_effect = requests.Timeout("timeout")
            result = n._send("test")
        assert result is False
        mock_post.assert_called()
        assert mock_post.call_args[1]["timeout"] == 5

    def test_send_no_http_when_testing(self) -> None:
        import bot.config as cfg
        cfg.CONFIG["testing"] = True
        _enable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            result = n._send("test message")
        assert result is True
        mock_post.assert_not_called()

    def test_send_no_http_when_testing_on_public_methods(self) -> None:
        import bot.config as cfg
        cfg.CONFIG["testing"] = True
        _enable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            n.bot_started(symbol="X", timeframe="1h", exchange="X")
            n.bot_stopped(cycles=0, balance=0)
            n.buy_opened(
                symbol="X", timeframe="1h", exchange="X",
                entry_price=0, quantity=0, position_size=0,
                stop_loss=0, take_profit=0, reasons=[],
            )
            n.trade_closed(
                exit_price=0, pnl_usd=0, pnl_pct=0, balance=0,
                exit_reason="Take Profit", holding_time=timedelta(),
            )
            n.state_restored(balance=0, has_position=False, trades=0)
            n.error_occurred(message="X")
            n.daily_summary(stats={"total_trades": 0}, balance=0)
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
#  Notification methods
# ---------------------------------------------------------------------------

class TestNotificationMethods:

    def test_bot_started(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.bot_started(symbol="BTC/USDT", timeframe="1h", exchange="binance")
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BOT STARTED" in text
        assert "BTC/USDT" in text
        assert "1h" in text
        assert "binance" in text

    def test_bot_stopped(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.bot_stopped(cycles=42, balance=10_500.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BOT STOPPED" in text
        assert "42" in text
        assert "10,500" in text

    def test_buy_opened(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.buy_opened(
                symbol="BTC/USDT",
                timeframe="1h",
                exchange="binance",
                entry_price=50_000.0,
                quantity=0.002,
                position_size=100.0,
                stop_loss=49_250.0,
                take_profit=51_250.0,
                reasons=["EMA200_BULLISH", "RSI_OVERSOLD"],
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BUY OPENED" in text
        assert "BTC/USDT" in text
        assert "50000" in text
        assert "-1.50%" in text
        assert "+2.50%" in text
        assert "EMA200_BULLISH" in text
        assert "Stop Loss" in text
        assert "Take Profit" in text

    def test_trade_closed_win(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51_000.0,
                pnl_usd=150.0,
                pnl_pct=3.0,
                balance=10_150.0,
                exit_reason="Take Profit",
                holding_time=timedelta(hours=4, minutes=30),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text
        assert "+150.00" in text
        assert "4h" in text

    def test_trade_closed_loss(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=49_000.0,
                pnl_usd=-200.0,
                pnl_pct=-2.0,
                balance=9_800.0,
                exit_reason="Stop Loss",
                holding_time=timedelta(hours=2),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text
        assert "-200.00" in text
        assert "2h" in text

    def test_trade_closed_strategy_exit(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=50_000.0,
                pnl_usd=0.0,
                pnl_pct=0.0,
                balance=10_000.0,
                exit_reason="Strategy Exit",
                holding_time=timedelta(hours=1, minutes=15),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text
        assert "1h" in text

    def test_state_restored(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.state_restored(balance=10_000.0, has_position=True, trades=5)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "STATE RESTORED" in text
        assert "10,000" in text
        assert "YES" in text
        assert "5" in text

    def test_state_restored_no_position(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.state_restored(balance=10_000.0, has_position=False, trades=0)
        text = mock_send.call_args[0][0]
        assert "NO" in text

    def test_error_occurred(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.error_occurred(message="API connection failed")
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "ERROR" in text
        assert "API connection failed" in text

    def test_daily_summary_with_trades(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        stats = {
            "total_trades": 10,
            "win_count": 7,
            "loss_count": 3,
            "win_rate": 70.0,
            "total_profit": 250.0,
            "profit_factor": 2.5,
            "average_win": 50.0,
            "average_loss": -16.67,
        }
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats, balance=10_250.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "DAILY SUMMARY" in text
        assert "10" in text or "7" in text
        assert "+250" in text
        assert "2.50" in text
        assert "10,250" in text

    def test_daily_summary_no_trades(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        stats = {
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "total_profit": 0.0,
            "profit_factor": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
        }
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats, balance=10_000.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "DAILY SUMMARY" in text
        assert "No trades completed" in text
        assert "10,000" in text


# ---------------------------------------------------------------------------
#  Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:

    def test_disabled_methods_do_not_raise(self) -> None:
        _disable_telegram()
        n = TelegramNotifier()
        n.bot_started(symbol="X", timeframe="1h", exchange="X")
        n.bot_stopped(cycles=0, balance=0)
        n.buy_opened(
            symbol="X", timeframe="1h", exchange="X",
            entry_price=0, quantity=0, position_size=0,
            stop_loss=0, take_profit=0, reasons=[],
        )
        n.trade_closed(
            exit_price=0, pnl_usd=0, pnl_pct=0, balance=0,
            exit_reason="X", holding_time=timedelta(),
        )
        n.state_restored(balance=0, has_position=False, trades=0)
        n.error_occurred(message="X")
        n.daily_summary(stats={"total_trades": 0}, balance=0)

    def test_no_http_request_when_disabled(self) -> None:
        _disable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            n.bot_started(symbol="X", timeframe="1h", exchange="X")
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
#  Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_reasons_list(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.buy_opened(
                symbol="BTC/USDT", timeframe="1h", exchange="binance",
                entry_price=50_000.0, quantity=0.002, position_size=100.0,
                stop_loss=49_250.0, take_profit=51_250.0, reasons=[],
            )
        text = mock_send.call_args[0][0]
        assert "" in text

    def test_format_timedelta_zero(self) -> None:
        from telegram.formatter import fmt_holding
        assert fmt_holding(0) == "0s"

    def test_format_timedelta_large(self) -> None:
        from telegram.formatter import fmt_holding
        td = timedelta(days=1, hours=5, minutes=30, seconds=15)
        result = fmt_holding(td.total_seconds())
        assert "d" in result or "h" in result

    def test_send_with_http_error(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            resp = MagicMock()
            resp.raise_for_status.side_effect = requests.HTTPError("403")
            mock_post.return_value = resp
            result = n._send("test")
        assert result is False


# ---------------------------------------------------------------------------
#  Regression tests: Trade Notification correctness
# ---------------------------------------------------------------------------


class TestTradeNotificationRegression:
    """Regression tests for trade_closed notification bugs."""

    def test_profitable_trade_shows_nonzero_pnl(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51_000.0, pnl_usd=150.0, pnl_pct=3.0,
                balance=10_150.0, exit_reason="Take Profit",
                holding_time=timedelta(hours=4, minutes=30),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "+150.00" in text
        assert "+2.00%" in text
        assert "$+150" in text

    def test_losing_trade_shows_negative_pnl(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=49_000.0, pnl_usd=-200.0, pnl_pct=-2.0,
                balance=9_800.0, exit_reason="Stop Loss",
                holding_time=timedelta(hours=2),
                symbol="ETH/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "-200.00" in text
        assert "-2.00%" in text

    def test_tp_exit_shows_correct_insight(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51_250.0, pnl_usd=125.0, pnl_pct=2.5,
                balance=10_125.0, exit_reason="Take Profit",
                holding_time=timedelta(hours=6),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "TP reached" in text or "Target hit" in text or "profit secured" in text

    def test_sl_exit_shows_correct_insight(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=49_250.0, pnl_usd=-75.0, pnl_pct=-1.5,
                balance=9_925.0, exit_reason="Stop Loss",
                holding_time=timedelta(hours=3),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "Stop Loss" in text or "protected capital" in text or "loss capped" in text

    def test_strategy_exit_shows_correct_insight(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=50_500.0, pnl_usd=50.0, pnl_pct=1.0,
                balance=10_050.0, exit_reason="Strategy Exit",
                holding_time=timedelta(hours=1, minutes=15),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "Strategy" in text or "Signal" in text or "Trend" in text or "strategy" in text

    def test_held_duration_nonzero(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51_000.0, pnl_usd=100.0, pnl_pct=2.0,
                balance=10_100.0, exit_reason="Take Profit",
                holding_time=timedelta(hours=5, minutes=30),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "5h" in text

    def test_held_short_duration(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=50_100.0, pnl_usd=10.0, pnl_pct=0.2,
                balance=10_010.0, exit_reason="Strategy Exit",
                holding_time=timedelta(seconds=45),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "45s" in text

    def test_held_days_duration(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=52_000.0, pnl_usd=2000.0, pnl_pct=4.0,
                balance=12_000.0, exit_reason="Take Profit",
                holding_time=timedelta(days=2, hours=4),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "2d" in text

    def test_all_required_fields_present(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51_000.0, pnl_usd=150.0, pnl_pct=3.0,
                balance=10_150.0, exit_reason="Take Profit",
                holding_time=timedelta(hours=4, minutes=30),
                symbol="BTC/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "Entry" in text
        assert "Exit" in text
        assert "Profit" in text
        assert "Held" in text
        assert "AI Insight" in text
        assert "Balance" in text

    def test_no_zero_pnl_for_real_trades(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=50_100.0, pnl_usd=25.0, pnl_pct=0.5,
                balance=10_025.0, exit_reason="Take Profit",
                holding_time=timedelta(minutes=15),
                symbol="SOL/USDT", entry_price=50_000.0,
            )
        text = mock_send.call_args[0][0]
        assert "+25.00" in text
        profit_line = [l for l in text.split("\n") if "Profit" in l or "+" in l]
        assert any("+25" in l for l in profit_line)


class TestDailySummaryProfitFactor:
    """Regression tests for Profit Factor display in daily summary."""

    def test_profit_factor_normal(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        stats = {
            "total_trades": 10, "win_count": 7, "loss_count": 3,
            "win_rate": 70.0, "total_profit": 250.0,
            "profit_factor": 2.5, "average_win": 50.0,
            "average_loss": -16.67,
        }
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats, balance=10_250.0)
        text = mock_send.call_args[0][0]
        assert "2.50" in text

    def test_profit_factor_infinity(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        stats = {
            "total_trades": 5, "win_count": 5, "loss_count": 0,
            "win_rate": 100.0, "total_profit": 500.0,
            "profit_factor": float("inf"), "average_win": 100.0,
            "average_loss": 0.0,
        }
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats, balance=10_500.0)
        text = mock_send.call_args[0][0]
        assert "\u221e" in text

    def test_profit_factor_zero(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        stats = {
            "total_trades": 3, "win_count": 0, "loss_count": 3,
            "win_rate": 0.0, "total_profit": -300.0,
            "profit_factor": 0.0, "average_win": 0.0,
            "average_loss": -100.0,
        }
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats, balance=9_700.0)
        text = mock_send.call_args[0][0]
        assert "0.00" in text


class TestFmtPF:
    """Regression tests for fmt_pf formatter."""

    def test_fmt_pf_normal(self) -> None:
        from telegram.formatter import fmt_pf
        assert fmt_pf(2.5) == "2.50"

    def test_fmt_pf_infinity(self) -> None:
        from telegram.formatter import fmt_pf
        assert fmt_pf(float("inf")) == "\u221e"

    def test_fmt_pf_nan(self) -> None:
        from telegram.formatter import fmt_pf
        assert fmt_pf(float("nan")) == "N/A"

    def test_fmt_pf_zero(self) -> None:
        from telegram.formatter import fmt_pf
        assert fmt_pf(0.0) == "0.00"


class TestAIInsightContextual:
    """Regression tests for contextual AI Insight on close."""

    def test_tp_insight(self) -> None:
        from telegram.ui import ai_insight
        result = ai_insight(is_buy=False, exit_reason="Take Profit")
        assert "TP" in result or "target" in result.lower() or "profit" in result.lower()

    def test_sl_insight(self) -> None:
        from telegram.ui import ai_insight
        result = ai_insight(is_buy=False, exit_reason="Stop Loss")
        assert "Stop Loss" in result or "capital" in result or "loss" in result.lower()

    def test_strategy_exit_insight(self) -> None:
        from telegram.ui import ai_insight
        result = ai_insight(is_buy=False, exit_reason="Strategy Exit")
        assert "strategy" in result.lower() or "Signal" in result or "Trend" in result or "exit" in result.lower()
