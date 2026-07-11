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
        assert "Bot Started" in text
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
        assert "Bot Stopped" in text
        assert "42" in text
        assert "10500" in text

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
        assert "50000" in text
        assert "0.002000" in text
        assert "100.00" in text
        assert "49250" in text
        assert "51250" in text
        assert "EMA200_BULLISH" in text
        assert "RSI_OVERSOLD" in text
        assert "Position Size" in text
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
        assert "*Take Profit*" in text
        assert "+150.00" in text
        assert "+3.00%" in text
        assert "10150" in text
        assert "WIN" in text
        assert "4h 30m" in text

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
        assert "*Stop Loss*" in text
        assert "-200.00" in text
        assert "-2.00%" in text
        assert "LOSS" in text

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
        assert "*Strategy Exit*" in text
        assert "1h 15m" in text

    def test_state_restored(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch.object(n, "_send") as mock_send:
            n.state_restored(balance=10_000.0, has_position=True, trades=5)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "State Restored" in text
        assert "10000" in text
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
        assert "Error" in text
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
        assert "Daily Summary" in text
        assert "10" in text
        assert "7" in text
        assert "3" in text
        assert "70.0%" in text
        assert "+250.00" in text
        assert "2.50" in text
        assert "+50.00" in text
        assert "-16.67" in text
        assert "10250" in text

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
        assert "Daily Summary" in text
        assert "No trades completed" in text
        assert "10000" in text


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
        assert TelegramNotifier._format_timedelta(timedelta()) == "00:00:00"

    def test_format_timedelta_large(self) -> None:
        td = timedelta(days=1, hours=5, minutes=30, seconds=15)
        assert TelegramNotifier._format_timedelta(td) == "29:30:15"

    def test_send_with_http_error(self) -> None:
        _enable_telegram()
        n = TelegramNotifier()
        with patch("bot.telegram.requests.post") as mock_post:
            resp = MagicMock()
            resp.raise_for_status.side_effect = requests.HTTPError("403")
            mock_post.return_value = resp
            result = n._send("test")
        assert result is False
