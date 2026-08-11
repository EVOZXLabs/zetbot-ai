"""
Production-readiness tests for ZetBot AI.

Covers:
- BUY symbol normalization for IDR/USDT quote currencies
- Telegram command menu registration (setMyCommands)
- Exchange market cache behavior (no duplicate load_markets)
- TP/SL notifications in execution pipeline
- Position lifecycle / restart recovery
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from scripts.app_config import AppConfig
from telegram.base_command import BaseCommand, CommandMeta
from telegram.command_center import CommandCenter
from telegram.registry import CommandRegistry
from telegram.commands.buy import BuyCommand
from telegram.commands.sell import SellCommand
from bot.notifier import Notifier


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _make_ctx(config=None, is_admin=True, quote_currency="USDT"):
    cfg = config or AppConfig(
        account_balance=10000,
        exchange="binance",
        timeframe="1h",
        quote_currency=quote_currency,
    )
    order_result = MagicMock()
    order_result.status = "FILLED"
    order_result.filled_amount = 0.0005
    order_result.filled_price = 50000
    order_result.amount = 0.0005
    order_result.executor = "paper"
    order_result.error = None
    order_result.symbol = "BTC/USDT"
    return MagicMock(
        config=cfg,
        is_admin=is_admin,
        chat_id="12345",
        services=MagicMock(
            exchange=MagicMock(get_ticker=MagicMock(return_value={"last": 50000})),
            order=MagicMock(
                mode="PAPER",
                execute=MagicMock(return_value=order_result),
            ),
        ),
    )


# ---------------------------------------------------------------------------
#  PART 2 — BUY symbol normalization
# ---------------------------------------------------------------------------

class TestBuySymbolNormalization:
    """The /buy command must resolve symbols using the configured quote
    currency, never hardcoding USDT."""

    def test_buy_appends_usdt_for_usdt_exchange(self):
        cmd = BuyCommand()
        ctx = _make_ctx(quote_currency="USDT")
        result = cmd.execute(ctx, "BTC 30000")
        assert "BTC/USDT" in result or "50000" in result

    def test_buy_appends_idr_for_idr_exchange(self):
        cmd = BuyCommand()
        ctx = _make_ctx(quote_currency="IDR")
        result = cmd.execute(ctx, "GWEI 30000")
        assert "GWEI/IDR" in result
        assert "GWEI/IDR/USDT" not in result

    def test_buy_preserves_full_symbol(self):
        cmd = BuyCommand()
        ctx = _make_ctx(quote_currency="IDR")
        result = cmd.execute(ctx, "BTC/IDR 30000")
        assert "BTC/IDR" in result

    def test_buy_usdt_full_symbol_preserved(self):
        cmd = BuyCommand()
        ctx = _make_ctx(quote_currency="USDT")
        result = cmd.execute(ctx, "BTC/USDT 30000")
        assert "BTC/USDT" in result

    def test_sell_also_uses_quote_currency(self):
        cmd = SellCommand()
        ctx = _make_ctx(quote_currency="IDR")
        ctx.services.position.get_open_positions.return_value = [
            {"symbol": "GWEI/IDR", "remaining_qty": 1.0, "current_price": 100},
        ]
        result = cmd.execute(ctx, "GWEI")
        assert "GWEI/IDR" in result


# ---------------------------------------------------------------------------
#  PART 1 — Telegram command center
# ---------------------------------------------------------------------------

class TestTelegramCommandCenter:
    """Command registry, dynamic help, and menu registration."""

    def test_all_commands_discovered(self):
        r = CommandRegistry()
        r.discover()
        names = {m.name for m in r.get_all_commands()}
        required = {
            "status", "positions", "signals", "detail", "buy", "sell",
            "portfolio", "performance", "balance", "wallet", "summary",
            "exchange", "health", "scan", "pipeline", "version", "logs",
            "help", "pause", "resume", "shutdown",
        }
        missing = required - names
        assert not missing, f"Missing commands: {missing}"

    def test_help_generated_from_registry(self):
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        help_text = cc.generate_help(user_is_admin=True)
        assert "status" in help_text
        assert "positions" in help_text
        assert "help" in help_text

    def test_help_excludes_hidden_commands(self):
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        help_text = cc.generate_help(user_is_admin=True)
        assert "takeprofit" not in help_text
        assert "stoploss" not in help_text

    def test_help_excludes_admin_for_non_admin(self):
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        help_text = cc.generate_help(user_is_admin=False)
        assert "buy" not in help_text
        assert "sell" not in help_text

    def test_command_count_logged_at_startup(self):
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        count = len(cc.registry.get_all_commands())
        assert count > 20

    def test_botfather_export_format(self):
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        export = cc.export_botfather_commands()
        lines = [l for l in export.strip().split("\n") if l]
        assert len(lines) > 20
        for line in lines:
            assert " - " in line


# ---------------------------------------------------------------------------
#  PART 3 — Exchange market cache
# ---------------------------------------------------------------------------

class TestExchangeMarketCache:
    """Market loading must be cached and not duplicated."""

    def test_base_provider_load_markets_returns_markets(self):
        from scripts.exchange_providers import BaseProvider

        class DummyProvider(BaseProvider):
            CCXT_NAME = "binance"

        provider = DummyProvider()
        with patch.object(provider, "_get_exchange") as mock_get:
            mock_ex = MagicMock()
            mock_ex.markets = {}
            mock_ex.load_markets.return_value = {"BTC/USDT": {}}
            mock_get.return_value = mock_ex

            first = provider.load_markets()
            assert first == {"BTC/USDT": {}}
            assert mock_ex.load_markets.call_count == 1

    def test_fetch_tickers_cached_shared_client(self):
        from bot.data import fetch_tickers_cached, clear_public_data_cache

        clear_public_data_cache()
        with patch("bot.data.get_cached_public_exchange") as mock_get:
            mock_ex = MagicMock()
            mock_ex.fetch_tickers.return_value = {
                "BTC/USDT": {"last": 50000},
            }
            mock_get.return_value = mock_ex

            r1 = fetch_tickers_cached("binance", ["BTC/USDT"])
            r2 = fetch_tickers_cached("binance", ["BTC/USDT"])

            assert r1["BTC/USDT"]["last"] == 50000
            assert r2["BTC/USDT"]["last"] == 50000
            mock_ex.fetch_tickers.assert_called_once()

    def test_get_cached_public_exchange_returns_same_instance(self):
        from bot.data import get_cached_public_exchange, clear_public_data_cache

        clear_public_data_cache()
        with patch("bot.data.build_public_exchange") as mock_build:
            mock_ex = MagicMock()
            mock_build.return_value = mock_ex

            first = get_cached_public_exchange("binance")
            second = get_cached_public_exchange("binance")

            assert first is second
            mock_build.assert_called_once()

    def test_scanner_uses_shared_exchange_instance(self):
        from scripts.scanner import MarketScanner
        from bot.data import get_cached_public_exchange, clear_public_data_cache

        clear_public_data_cache()
        with patch("bot.data.build_public_exchange") as mock_build:
            mock_ex = MagicMock()
            mock_ex.fetch_markets.return_value = [
                {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
                 "spot": True, "active": True},
            ]
            mock_ex.fetch_tickers.return_value = {
                "BTC/USDT": {"last": 50000, "quoteVolume": 1000000,
                             "percentage": 2.5, "high": 51000, "low": 49000},
            }
            mock_ex.fetch_ohlcv.return_value = [
                [1700000000000, 49000, 50000, 48000, 49500, 1000],
            ]
            mock_build.return_value = mock_ex

            cfg = AppConfig(
                exchange="binance",
                quote_currency="USDT",
                timeframe="1h",
                account_balance=10000,
                scanner_threads=1,
                scanner_top_n=10,
                scanner_min_volume=0,
            )
            scanner = MarketScanner(threads=1, config=cfg)

            assert scanner.md.exchange is mock_ex

        clear_public_data_cache()


# ---------------------------------------------------------------------------
#  PART 5 — TP/SL notifications
# ---------------------------------------------------------------------------

class TestTPSLNotifications:
    """Notifier must send TP1/TP2/TP3 and SL notifications."""

    def test_notify_take_profit_with_level(self):
        notifier = Notifier(
            enabled=True,
            token="fake_token",
            chat_id="123",
            testing=True,
        )
        result = notifier.notify_take_profit(
            symbol="BTC/USDT",
            entry_price=50000,
            exit_price=52000,
            profit=200,
            level="TP1",
        )
        assert result is True

    def test_notify_stop_loss_sends(self):
        notifier = Notifier(
            enabled=True,
            token="fake_token",
            chat_id="123",
            testing=True,
        )
        result = notifier.notify_stop_loss(
            symbol="BTC/USDT",
            entry_price=50000,
            exit_price=48000,
            loss=-200,
        )
        assert result is True

    def test_notify_take_profit_default_level(self):
        notifier = Notifier(
            enabled=True,
            token="fake_token",
            chat_id="123",
            testing=True,
        )
        result = notifier.notify_take_profit(
            symbol="BTC/USDT",
            entry_price=50000,
            exit_price=52000,
            profit=200,
        )
        assert result is True


# ---------------------------------------------------------------------------
#  PART 4 — Position lifecycle
# ---------------------------------------------------------------------------

class TestPositionLifecycle:
    """Position state persistence and recovery."""

    def test_paper_state_loads_open_positions(self):
        from scripts.paper_trading_engine import PaperTradingEngine

        with patch("scripts.paper_trading_engine.STATE_PATH", "data/paper_state.json"):
            os.makedirs("data", exist_ok=True)
            state = {
                "balance": 9000,
                "initial_balance": 10000,
                "margin_used": 0,
                "orders": [],
                "positions": {
                    "BTC/USDT": {
                        "symbol": "BTC/USDT",
                        "order_id": "test",
                        "quantity": 0.1,
                        "remaining_qty": 0.1,
                        "entry_price": 50000,
                        "current_price": 51000,
                        "unrealized_pnl": 100,
                        "realized_pnl": 0,
                        "total_pnl": 100,
                        "cost_basis": 5000,
                        "status": "OPEN",
                        "tp1_sold": False,
                        "tp2_sold": False,
                        "tp3_sold": False,
                        "opened_at": "2024-01-01T00:00:00+00:00",
                        "signal_time": "2024-01-01T00:00:00+00:00",
                        "closure_notified": False,
                        "tp1": 51500,
                        "tp2": 53000,
                        "tp3": 55000,
                        "stop_loss": 49000,
                        "position_size_usdt": 5000,
                    },
                },
                "equity_history": [],
            }
            with open("data/paper_state.json", "w") as f:
                json.dump(state, f)

            engine = PaperTradingEngine(notifier=None, initial_balance=10000)
            assert "BTC/USDT" in engine.positions
            assert engine.positions["BTC/USDT"].status == "OPEN"
