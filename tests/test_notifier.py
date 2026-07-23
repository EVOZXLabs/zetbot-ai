"""Unit tests for the centralized Notifier (bot/notifier.py).

Covers: initialization, from_config, from_env, all notification methods,
backward compatibility, retry logic, graceful degradation.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot.notifier import Notifier


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

def _make_notifier(**kwargs: object) -> Notifier:
    defaults: dict[str, object] = {
        "enabled": True,
        "token": "123:ABC",
        "chat_id": "456",
        "timeout": 10,
        "max_retry": 3,
        "testing": False,
    }
    defaults.update(kwargs)
    return Notifier(**defaults)


# ---------------------------------------------------------------------------
#  Initialization
# ---------------------------------------------------------------------------

class TestNotifierInit:

    def test_disabled_by_default(self) -> None:
        n = Notifier()
        assert not n.enabled

    def test_disabled_when_credentials_missing(self) -> None:
        n = Notifier(enabled=True, token="", chat_id="")
        assert not n.enabled

    def test_enabled_with_credentials(self) -> None:
        n = _make_notifier()
        assert n.enabled

    def test_disabled_when_token_missing(self) -> None:
        n = _make_notifier(token="")
        assert not n.enabled

    def test_disabled_when_chat_id_missing(self) -> None:
        n = _make_notifier(chat_id="")
        assert not n.enabled


# ---------------------------------------------------------------------------
#  from_config / from_env
# ---------------------------------------------------------------------------

class TestFactoryMethods:

    def test_from_config(self) -> None:
        cfg = MagicMock()
        cfg.telegram_enabled = True
        cfg.telegram_token = "tok"
        cfg.telegram_chat_id = "cid"
        cfg.telegram_timeout = 5
        cfg.telegram_retry = 2
        cfg.testing = False
        n = Notifier.from_config(cfg)
        assert n.enabled
        assert n._token == "tok"
        assert n._chat_id == "cid"
        assert n._timeout == 5
        assert n._max_retry == 2

    def test_from_config_disabled(self) -> None:
        cfg = MagicMock()
        cfg.telegram_enabled = False
        n = Notifier.from_config(cfg)
        assert not n.enabled

    def test_from_env(self) -> None:
        with patch.dict("os.environ", {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_TOKEN": "envtok",
            "TELEGRAM_CHAT_ID": "envchat",
            "TELEGRAM_TIMEOUT": "7",
            "TELEGRAM_RETRY": "2",
            "TESTING": "false",
        }):
            n = Notifier.from_env()
            assert n.enabled
            assert n._token == "envtok"
            assert n._chat_id == "envchat"
            assert n._timeout == 7
            assert n._max_retry == 2

    def test_from_env_disabled(self) -> None:
        with patch.dict("os.environ", {"TELEGRAM_ENABLED": "false"}):
            n = Notifier.from_env()
            assert not n.enabled


# ---------------------------------------------------------------------------
#  _send
# ---------------------------------------------------------------------------

class TestSend:

    def test_send_disabled_returns_false(self) -> None:
        n = _make_notifier(enabled=False)
        assert n._send("hello") is False

    def test_send_success(self) -> None:
        n = _make_notifier()
        with patch("bot.notifier.requests.post") as mock_post:
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
        n = _make_notifier(max_retry=3)
        with patch("bot.notifier.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("fail")
            result = n._send("test")
        assert result is False
        assert mock_post.call_count == 3

    def test_send_succeeds_after_retry(self) -> None:
        n = _make_notifier(max_retry=3)
        with patch("bot.notifier.requests.post") as mock_post:
            mock_post.side_effect = [
                requests.RequestException("fail 1"),
                MagicMock(ok=True),
            ]
            mock_post.return_value.raise_for_status.return_value = None
            result = n._send("test")
        assert result is True
        assert mock_post.call_count == 2

    def test_send_testing_mode(self) -> None:
        n = _make_notifier(testing=True)
        with patch("bot.notifier.requests.post") as mock_post:
            result = n._send("test message")
        assert result is True
        mock_post.assert_not_called()

    def test_send_raw(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.send("raw message")
        mock_send.assert_called_once_with("raw message")


# ---------------------------------------------------------------------------
#  Notification methods
# ---------------------------------------------------------------------------

class TestNotificationMethods:

    def test_notify_buy_opened(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_buy_opened(
                symbol="BTC/USDT", exchange="binance", timeframe="1h",
                entry_price=50_000.0, quantity=0.002, position_size=100.0,
                stop_loss=49_250.0, take_profit=51_250.0,
                reasons=["EMA200_BULLISH"],
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BUY OPENED" in text
        assert "BTC/USDT" in text
        assert "50000" in text
        assert "-1.50%" in text
        assert "+2.50%" in text
        assert "EMA200_BULLISH" in text

    def test_notify_position_closed_win(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="BTC/USDT", entry_price=50_000.0, exit_price=51_000.0,
                pnl=150.0, pnl_pct=3.0, balance=10_150.0,
                exit_reason="Take Profit",
                holding_time=timedelta(hours=4, minutes=30),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text
        assert "+150.00" in text
        assert "4h" in text

    def test_notify_position_closed_loss(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="ETH/USDT", entry_price=50_000.0, exit_price=49_000.0,
                pnl=-200.0, pnl_pct=-2.0, balance=9_800.0,
                exit_reason="Stop Loss",
                holding_time=timedelta(hours=2),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text
        assert "-200.00" in text

    def test_notify_take_profit(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_take_profit(
                symbol="BTC/USDT", entry_price=50_000.0, exit_price=51_250.0,
                profit=125.0, holding_time=timedelta(hours=6),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "TAKE PROFIT HIT" in text
        assert "+125" in text

    def test_notify_stop_loss(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_stop_loss(
                symbol="BTC/USDT", entry_price=50_000.0, exit_price=49_250.0,
                loss=-75.0, holding_time=timedelta(hours=3),
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "STOP LOSS HIT" in text
        assert "-75" in text

    def test_notify_trade_rejected(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_trade_rejected(
                symbol="SOL/USDT", reason="R:R below minimum",
            )
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "TRADE REJECTED" in text
        assert "SOL/USDT" in text
        assert "R:R below minimum" in text

    def test_notify_bot_started(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_started("BTC/USDT", "1h", "binance")
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BOT STARTED" in text
        assert "BTC/USDT" in text

    def test_notify_bot_stopped(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_bot_stopped(cycles=42, balance=10_500.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "BOT STOPPED" in text
        assert "42" in text
        assert "10,500" in text

    def test_notify_error(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_error("API connection failed")
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "ERROR" in text
        assert "API connection failed" in text

    def test_notify_system(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_system("Pipeline completed")
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "SYSTEM" in text
        assert "Pipeline completed" in text

    def test_notify_daily_summary_with_trades(self) -> None:
        n = _make_notifier()
        stats = {
            "total_trades": 10, "win_count": 7, "loss_count": 3,
            "win_rate": 70.0, "total_profit": 250.0,
            "profit_factor": 2.5,
        }
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=10_250.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "DAILY SUMMARY" in text
        assert "2.50" in text
        assert "10,250" in text

    def test_notify_daily_summary_no_trades(self) -> None:
        n = _make_notifier()
        stats = {"total_trades": 0}
        with patch.object(n, "_send") as mock_send:
            n.notify_daily_summary(stats, balance=10_000.0)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "No trades completed" in text

    def test_notify_state_restored(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_state_restored(balance=10_000.0, has_position=True, trades=5)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "STATE RESTORED" in text
        assert "YES" in text


# ---------------------------------------------------------------------------
#  Backward-compatible aliases
# ---------------------------------------------------------------------------

class TestBackwardCompatAliases:

    def test_bot_started_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.bot_started(symbol="X", timeframe="1h", exchange="X")
        mock_send.assert_called_once()
        assert "BOT STARTED" in mock_send.call_args[0][0]

    def test_bot_stopped_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.bot_stopped(cycles=5, balance=10000)
        mock_send.assert_called_once()
        assert "BOT STOPPED" in mock_send.call_args[0][0]

    def test_buy_opened_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.buy_opened(
                symbol="BTC/USDT", timeframe="1h", exchange="binance",
                entry_price=50000, quantity=0.002, position_size=100,
                stop_loss=49250, take_profit=51250, reasons=["test"],
            )
        mock_send.assert_called_once()
        assert "BUY OPENED" in mock_send.call_args[0][0]

    def test_trade_closed_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.trade_closed(
                exit_price=51000, pnl_usd=150, pnl_pct=3.0,
                balance=10150, exit_reason="Take Profit",
                holding_time=timedelta(hours=4),
                symbol="BTC/USDT", entry_price=50000,
            )
        mock_send.assert_called_once()
        assert "POSITION CLOSED" in mock_send.call_args[0][0]

    def test_state_restored_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.state_restored(balance=10000, has_position=False, trades=0)
        mock_send.assert_called_once()
        assert "STATE RESTORED" in mock_send.call_args[0][0]

    def test_error_occurred_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.error_occurred(message="test error")
        mock_send.assert_called_once()
        assert "ERROR" in mock_send.call_args[0][0]

    def test_daily_summary_alias(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.daily_summary(stats={"total_trades": 0}, balance=10000)
        mock_send.assert_called_once()
        assert "DAILY SUMMARY" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
#  Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:

    def test_disabled_methods_do_not_raise(self) -> None:
        n = _make_notifier(enabled=False)
        n.notify_buy_opened(symbol="X")
        n.notify_position_closed(symbol="X")
        n.notify_take_profit(symbol="X")
        n.notify_stop_loss(symbol="X")
        n.notify_trade_rejected(symbol="X", reason="test")
        n.notify_bot_started()
        n.notify_bot_stopped()
        n.notify_error("X")
        n.notify_system("X")
        n.notify_daily_summary({}, 0)
        n.notify_state_restored()

    def test_no_http_when_disabled(self) -> None:
        n = _make_notifier(enabled=False)
        with patch("bot.notifier.requests.post") as mock_post:
            n.notify_bot_started("X", "1h", "X")
        mock_post.assert_not_called()

    def test_exceptions_never_propagate(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send", side_effect=RuntimeError("boom")):
            assert n.notify_buy_opened(symbol="X") is False
            assert n.notify_position_closed(symbol="X") is False
            assert n.notify_take_profit(symbol="X") is False
            assert n.notify_stop_loss(symbol="X") is False
            assert n.notify_trade_rejected(symbol="X", reason="X") is False
            assert n.notify_bot_started() is False
            assert n.notify_bot_stopped() is False
            assert n.notify_error("X") is False
            assert n.notify_system("X") is False
            assert n.notify_daily_summary({}, 0) is False
            assert n.notify_state_restored() is False


# ---------------------------------------------------------------------------
#  Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_reasons_list(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_buy_opened(
                symbol="BTC/USDT", entry_price=50000,
                quantity=0.002, position_size=100,
                stop_loss=49250, take_profit=51250, reasons=[],
            )
        text = mock_send.call_args[0][0]
        assert "" in text

    def test_none_holding_time_defaults(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="X", entry_price=100, exit_price=110,
                pnl=10, exit_reason="Take Profit",
            )
        text = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in text

    def test_zero_prices(self) -> None:
        n = _make_notifier()
        with patch.object(n, "_send") as mock_send:
            n.notify_buy_opened(symbol="X")
        text = mock_send.call_args[0][0]
        assert "BUY OPENED" in text
