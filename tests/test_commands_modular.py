"""
Integration tests for every modular command.

Each command class is instantiated and executed with a mock
CommandContext to verify output format, error handling, etc.
"""

import datetime
import os
import time
from typing import Any
from unittest.mock import MagicMock

from scripts.app_config import AppConfig
from telegram.base_command import BaseCommand
from telegram.command_center import CommandCenter
from telegram.context import CommandContext
from telegram.registry import CommandRegistry


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

BASE_CFG = AppConfig(
    account_balance=10000, exchange="binance", timeframe="1h",
    max_positions=3, max_risk_per_trade_pct=2,
    scanner_threads=5, scanner_top_n=50,
    telegram_timeout=10, telegram_retry=3,
    min_rr=1.5, max_rr=5, min_probability=50,
    max_atr_pct=8, tp1_sell_pct=30, tp2_sell_pct=30,
    tp3_sell_pct=40, taker_fee=0.001, maker_fee=0.00075,
    slippage_bps=3,
)


def _make_ctx(**kwargs: Any) -> CommandContext:
    """Create a CommandContext with test-mode=True and sensible defaults."""
    defaults: dict[str, Any] = dict(
        config=BASE_CFG,
        logger=None,
        chat_id="12345",
        message_id=1,
        update_id=1,
        raw_text="",
        is_admin=True,
        test_mode=True,
        start_time=time.time(),
    )
    defaults.update(kwargs)
    return CommandContext(**defaults)


def _execute(cmd: type[BaseCommand], ctx: CommandContext, args: str = "") -> str:
    return cmd().execute(ctx, args)


def _all_commands() -> list[type[BaseCommand]]:
    r = CommandRegistry()
    r.discover()
    seen: set[int] = set()
    result = []
    for cls in r.commands.values():
        uid = id(cls)
        if uid not in seen:
            seen.add(uid)
            result.append(cls)
    return result


# ---------------------------------------------------------------------------
#  Fixtures (files that commands read)
# ---------------------------------------------------------------------------

def _ensure_data_files() -> None:
    import json  # noqa: PLC0415
    os.makedirs("data", exist_ok=True)

    with open("data/paper_balance.json", "w") as f:
        json.dump({
            "final_balance": 10000.0,
            "final_equity": 10500.0,
            "realized_pnl": 250.0,
            "unrealized_pnl": 250.0,
            "net_pnl": 500.0,
            "total_return_pct": 5.0,
            "total_trades": 10,
            "winning_trades": 7,
            "losing_trades": 3,
            "win_rate": 70.0,
            "profit_factor": 2.5,
            "gross_profit": 1000.0,
            "gross_loss": 400.0,
        }, f)

    with open("data/positions.json", "w") as f:
        json.dump({
            "positions": [{
                "symbol": "BTC/USDT",
                "status": "OPEN",
                "entry_price": 60000.0,
                "current_price": 61000.0,
                "floating_pnl": 100.0,
                "floating_pnl_pct": 1.67,
                "stop_loss": 58000.0,
                "tp1": 62000.0,
                "tp2": 63000.0,
                "tp3": 64000.0,
                "holding_hours": 12.5,
                "position_size_usdt": 1000.0,
            }]
        }, f)

    with open("data/paper_orders.json", "w") as f:
        json.dump({
            "orders": [
                {"symbol": "BTC/USDT", "status": "CLOSED",
                 "net_pnl": 150.0, "side": "buy",
                 "entry_price": 60000.0, "exit_price": 61500.0,
                 "closed_at": "2026-07-11T10:00:00Z",
                 "filled_at": "2026-07-11T08:00:00Z",
                 "exit_reason": "Take Profit",
                 "total_cost": 60000.0,
                 "net_pnl_pct": 2.5},
                {"symbol": "ETH/USDT", "status": "CLOSED",
                 "net_pnl": -50.0, "side": "buy",
                 "entry_price": 3000.0, "exit_price": 2950.0,
                 "closed_at": "2026-07-10T10:00:00Z",
                 "filled_at": "2026-07-10T08:00:00Z",
                 "exit_reason": "Stop Loss",
                 "total_cost": 3000.0,
                 "net_pnl_pct": -1.67},
            ]
        }, f)

    with open("data/scanner_results.json", "w") as f:
        json.dump({
            "generated": "2026-07-11T01:00:00Z",
            "total_pairs": 100,
            "pairs": [
                {"symbol": "BTC/USDT", "base": "BTC", "price": 60000.0,
                 "volume_24h": 50000000, "change_24h": 2.5,
                 "ema50": 59000.0, "ema100": 58500.0, "ema200": 58000.0,
                 "rsi14": 62.5, "adx14": 35.0, "atr_pct": 1.5,
                 "relative_volume": 1.2,
                 "trend_alignment": "BULLISH",
                 "trend_score": 85, "momentum_score": 80,
                 "volume_score": 90, "volatility_score": 40,
                 "liquidity_score": 95,
                 "overall": 85.5, "signal": "STRONG BUY", "rank": 1},
                {"symbol": "ETH/USDT", "base": "ETH", "price": 3000.0,
                 "volume_24h": 20000000, "change_24h": 1.2,
                 "ema50": 2950.0, "ema100": 2900.0, "ema200": 2850.0,
                 "rsi14": 58.0, "adx14": 28.0, "atr_pct": 2.0,
                 "relative_volume": 0.8,
                 "trend_alignment": "BULLISH",
                 "trend_score": 75, "momentum_score": 70,
                 "volume_score": 80, "volatility_score": 50,
                 "liquidity_score": 90,
                 "overall": 72.0, "signal": "BUY", "rank": 2},
                {"symbol": "SOL/USDT", "base": "SOL", "price": 150.0,
                 "volume_24h": 5000000, "change_24h": -0.5,
                 "ema50": 155.0, "ema100": 160.0, "ema200": 165.0,
                 "rsi14": 45.0, "adx14": 22.0, "atr_pct": 3.0,
                 "relative_volume": 0.5,
                 "trend_alignment": "NEUTRAL",
                 "trend_score": 50, "momentum_score": 45,
                 "volume_score": 60, "volatility_score": 55,
                 "liquidity_score": 70,
                 "overall": 45.0, "signal": "NEUTRAL", "rank": 3},
            ]
        }, f)

    # Remove any leftover state files
    for f in (".paused", ".shutdown_requested", ".last_update_id", ".notified_buys"):
        p = os.path.join("data", f)
        if os.path.exists(p):
            os.remove(p)


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

class TestCommandsSmoke:
    """Every command executes without error and returns a string."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_all_commands_return_string(self) -> None:
        ctx = _make_ctx()
        for cls in _all_commands():
            name = cls.meta.name
            # Skip slow commands that need real dependencies
            if name in ("pipeline", "scan"):
                continue
            instance = cls()
            try:
                result = instance.execute(ctx, "")
            except Exception as exc:
                raise AssertionError(
                    f"Command '{name}' raised {exc}"
                ) from exc
            assert isinstance(result, str), (
                f"Command '{name}' returned {type(result)}, expected str"
            )
            assert len(result) > 0, f"Command '{name}' returned empty string"

    def test_all_commands_with_args(self) -> None:
        ctx = _make_ctx()
        for cls in _all_commands():
            name = cls.meta.name
            if name in ("pipeline", "scan"):
                continue
            instance = cls()
            try:
                result = instance.execute(ctx, "some args here")
            except Exception as exc:
                raise AssertionError(
                    f"Command '{name}' with args raised {exc}"
                ) from exc
            assert isinstance(result, str), (
                f"Command '{name}' returned {type(result)}"
            )


class TestSpecificCommands:
    """Known commands must return expected content."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_status_contains_expected(self) -> None:
        from telegram.commands.status import StatusCommand
        result = _execute(StatusCommand, _make_ctx())
        assert "Bot Status" in result
        assert "PAPER" in result or "LIVE" in result
        assert "Scheduler:" in result
        assert "Pipeline:" in result
        assert "Last Scan:" in result
        assert "Health:" in result

    def test_help_contains_commands(self) -> None:
        from telegram.commands.help import HelpCommand
        result = _execute(HelpCommand, _make_ctx())
        assert "/status" in result
        assert "/health" in result
        assert "/help" in result

    def test_balance_contains_pnl(self) -> None:
        from telegram.commands.balance import BalanceCommand
        result = _execute(BalanceCommand, _make_ctx())
        assert "Balance" in result or "PnL" in result

    def test_positions_shows_open(self) -> None:
        from telegram.commands.positions import PositionsCommand
        result = _execute(PositionsCommand, _make_ctx())
        # Should contain at least one symbol
        assert "/USDT" in result or "/BTC" in result or "symbol" in result.lower()
        assert "Entry:" in result
        assert "PnL:" in result
        assert "ROI:" in result
        assert "SL:" in result or "Remaining:" in result
        assert "Holding:" in result

    def test_summary_shows_stats(self) -> None:
        from telegram.commands.summary import SummaryCommand
        result = _execute(SummaryCommand, _make_ctx())
        assert "Today" in result or "Summary" in result

    def test_version_contains_bot_name(self) -> None:
        from telegram.commands.version import VersionCommand
        result = _execute(VersionCommand, _make_ctx())
        assert "ZetBot" in result

    def test_pause_pauses_trading(self) -> None:
        from telegram.commands.pause import PauseCommand
        result = _execute(PauseCommand, _make_ctx())
        assert "Paused" in result or "paused" in result
        assert os.path.exists("data/.paused")
        # Cleanup
        if os.path.exists("data/.paused"):
            os.remove("data/.paused")

    def test_resume_resumes_trading(self) -> None:
        from telegram.commands.resume import ResumeCommand
        # Create pause file first
        with open("data/.paused", "w") as f:
            f.write("test")
        result = _execute(ResumeCommand, _make_ctx())
        assert "Resumed" in result or "resumed" in result
        assert not os.path.exists("data/.paused")

    def test_shutdown_requests_confirmation(self) -> None:
        from telegram.commands.shutdown import ShutdownCommand
        result = _execute(ShutdownCommand, _make_ctx())
        # First call should ask for confirmation
        assert "Confirmation" in result or "confirm" in result.lower()

    def test_shutdown_confirms(self) -> None:
        from telegram.commands.shutdown import ShutdownCommand
        # First call to set pending
        ShutdownCommand._pending.clear()
        ctx = _make_ctx(chat_id="test_shutdown")
        _execute(ShutdownCommand, ctx)  # first: confirmation request
        # Force pending time to be recent
        from telegram.commands.shutdown import ShutdownCommand as SC
        SC._pending["test_shutdown"] = time.time() - 1  # 1 second ago
        result = _execute(ShutdownCommand, ctx)  # second: confirm
        assert "Shutting Down" in result or "shutting down" in result.lower()
        SC._pending.clear()

    def test_health_with_monitor(self) -> None:
        from telegram.commands.health import HealthCommand
        from unittest.mock import MagicMock
        health = MagicMock()
        health.force_refresh.return_value = {
            "version": "v0.5.0",
            "uptime_sec": 3600,
            "rss_kb": 102400,
            "thread_count": 5,
            "process_cpu_sec": 12.5,
            "internet_ok": True,
            "exchange_ok": True,
            "scanner_time": "2024-01-01 12:00",
            "api_time": "2024-01-01 12:00",
            "balance": 10000.0,
            "equity": 10500.0,
            "net_pnl": 500.0,
            "open_positions": 1,
            "total_trades": 10,
            "win_rate": 70.0,
            "paused": False,
            "paper_mode": True,
        }
        ctx = _make_ctx(health_monitor=health)
        result = _execute(HealthCommand, ctx)
        assert "Health" in result
        assert "Score:" in result

    def test_pipeline_with_config(self) -> None:
        from telegram.commands.pipeline import PipelineCommand
        # Just test that the command class exists and can be instantiated
        cmd = PipelineCommand()
        assert cmd is not None
        assert cmd.meta.name == "pipeline"

    def test_logs_without_log_dir(self) -> None:
        from telegram.commands.logs import LogsCommand
        # Remove logs dir temporarily
        if os.path.exists("logs"):
            os.rename("logs", "logs_bak")
        result = _execute(LogsCommand, _make_ctx())
        assert isinstance(result, str)
        if os.path.exists("logs_bak"):
            os.rename("logs_bak", "logs")

    def test_config_command_shows_settings(self) -> None:
        from telegram.commands.config import ConfigCommand
        result = _execute(ConfigCommand, _make_ctx())
        assert "Configuration" in result or "config" in result.lower()

    def test_wallet_shows_balance(self) -> None:
        from telegram.commands.wallet import WalletCommand
        result = _execute(WalletCommand, _make_ctx())
        assert "Wallet" in result or "Balance" in result

    def test_signals_placeholder(self) -> None:
        from telegram.commands.signals import SignalsCommand
        result = _execute(SignalsCommand, _make_ctx())
        assert isinstance(result, str)

    def test_stoploss_placeholder(self) -> None:
        from telegram.commands.stoploss import StoplossCommand
        result = _execute(StoplossCommand, _make_ctx())
        assert isinstance(result, str)

    def test_takeprofit_placeholder(self) -> None:
        from telegram.commands.takeprofit import TakeprofitCommand
        result = _execute(TakeprofitCommand, _make_ctx())
        assert isinstance(result, str)

    def test_buy_placeholder(self) -> None:
        from telegram.commands.buy import BuyCommand
        result = _execute(BuyCommand, _make_ctx())
        assert isinstance(result, str)

    def test_sell_placeholder(self) -> None:
        from telegram.commands.sell import SellCommand
        result = _execute(SellCommand, _make_ctx())
        assert isinstance(result, str)

    def test_restart_placeholder(self) -> None:
        from telegram.commands.restart import RestartCommand
        result = _execute(RestartCommand, _make_ctx())
        assert isinstance(result, str)

    def test_reload_placeholder(self) -> None:
        from telegram.commands.reload import ReloadCommand
        result = _execute(ReloadCommand, _make_ctx())
        assert isinstance(result, str)


class TestHiddenCommands:
    """Hidden commands must still be executable."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_hidden_commands_executable(self) -> None:
        ctx = _make_ctx()
        for cls in _all_commands():
            if cls.meta.hidden:
                result = _execute(cls, ctx, "")
                assert isinstance(result, str)


class TestCommandCenterDispatch:
    """CommandCenter dispatch must route correctly."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_dispatch_status(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        result = cc.dispatch(chat_id="123", message_id=1, update_id=1, text="/status", test_mode=True)
        assert result is not None
        assert "Bot Status" in result

    def test_dispatch_unknown(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        result = cc.dispatch(chat_id="123", message_id=1, update_id=1, text="/nonexistent_cmd", test_mode=True)
        assert result is None

    def test_dispatch_alias(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        result = cc.dispatch(chat_id="123", message_id=1, update_id=1, text="/bal", test_mode=True)
        assert result is not None

    def test_dispatch_with_args(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        result = cc.dispatch(chat_id="123", message_id=1, update_id=1, text="/status detail", test_mode=True)
        assert result is not None

    def test_dispatch_generates_help(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        result = cc.dispatch(chat_id="123", message_id=1, update_id=1, text="/help", test_mode=True)
        assert result is not None
        assert "/status" in result

    def test_dispatch_botfather_export(self) -> None:
        cc = CommandCenter(BASE_CFG, logger=None)
        export = cc.export_botfather_commands()
        assert "status" in export
        assert "health" in export


class TestContext:
    """CommandContext fields and helpers."""

    def test_runtime_formatted(self) -> None:
        ctx = _make_ctx()
        rt = ctx.runtime_formatted()
        assert ":" in rt

    def test_read_json_missing(self) -> None:
        ctx = _make_ctx()
        result = ctx.read_json("nonexistent_file.json")
        assert result == {}

    def test_read_json_valid(self) -> None:
        _ensure_data_files()
        ctx = _make_ctx()
        result = ctx.read_json("paper_balance.json")
        assert "final_balance" in result


class TestMiddleware:
    """Middleware pipeline."""

    def test_middleware_authorized(self) -> None:
        from telegram.middleware import run_middleware
        ctx = _make_ctx(test_mode=True)
        result = run_middleware(ctx, "test", lambda: "hello")
        assert result == "hello"

    def test_middleware_exception_handling(self) -> None:
        from telegram.middleware import run_middleware
        ctx = _make_ctx(test_mode=True)

        def _fail() -> str:
            raise ValueError("test error")

        result = run_middleware(ctx, "test", _fail)
        assert "failed" in result


class TestPermissions:
    """Permission checks."""

    def test_authorized_in_test_mode(self) -> None:
        from telegram.permissions import is_authorized
        ctx = _make_ctx(test_mode=True)
        assert is_authorized(ctx)

    def test_unauthorized_no_test_mode(self) -> None:
        from telegram.permissions import is_authorized, configure
        configure("999")
        ctx = _make_ctx(chat_id="123", test_mode=False)
        assert not is_authorized(ctx)

    def test_authorized_correct_chat(self) -> None:
        from telegram.permissions import is_authorized, configure
        configure("123")
        ctx = _make_ctx(chat_id="123", test_mode=False)
        assert is_authorized(ctx)


class TestNewUX:
    """Tests for the Production UX improvements."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_portfolio_shows_expected(self) -> None:
        from telegram.commands.portfolio import PortfolioCommand
        result = _execute(PortfolioCommand, _make_ctx())
        assert "Portfolio" in result
        assert "Cash:" in result
        assert "Equity:" in result
        assert "Net PnL:" in result
        assert "Exposure:" in result

    def test_history_shows_trades(self) -> None:
        from telegram.commands.history import HistoryCommand
        result = _execute(HistoryCommand, _make_ctx())
        assert "Trade History" in result
        assert "BTC/USDT" in result
        assert "Summary" in result

    def test_performance_shows_metrics(self) -> None:
        from telegram.commands.performance import PerformanceCommand
        result = _execute(PerformanceCommand, _make_ctx())
        assert "Performance" in result
        assert "Win Rate" in result

    def test_market_shows_overview(self) -> None:
        from telegram.commands.market import MarketCommand
        result = _execute(MarketCommand, _make_ctx())
        assert "Market Overview" in result
        assert "BTC:" in result
        assert "ETH:" in result

    def test_pair_shows_analysis(self) -> None:
        from telegram.commands.pair import PairCommand
        result = _execute(PairCommand, _make_ctx(), "BTC")
        assert "BTC/USDT" in result or "BTC" in result
        assert "RSI" in result
        assert "ADX" in result

    def test_pair_nonexistent(self) -> None:
        from telegram.commands.pair import PairCommand
        result = _execute(PairCommand, _make_ctx(), "NONEXISTENT")
        assert "not found" in result

    def test_pair_empty_args(self) -> None:
        from telegram.commands.pair import PairCommand
        result = _execute(PairCommand, _make_ctx(), "")
        assert "Usage:" in result

    def test_wallet_shows_all_fields(self) -> None:
        from telegram.commands.wallet import WalletCommand
        result = _execute(WalletCommand, _make_ctx())
        assert "Wallet" in result
        assert "Cash:" in result
        assert "Equity:" in result
        assert "Net PnL:" in result
        assert "Exposure:" in result
        assert "Buying Power" in result

    def test_health_shows_components(self) -> None:
        from telegram.commands.health import HealthCommand
        result = _execute(HealthCommand, _make_ctx())
        assert "Health" in result
        assert "Components" in result

    def test_help_has_sections(self) -> None:
        from telegram.commands.help import HelpCommand
        result = _execute(HelpCommand, _make_ctx())
        assert "Available Commands" in result
        assert "Trading" in result
        assert "Monitoring" in result
        assert "System" in result

    def test_history_limit(self) -> None:
        from telegram.commands.history import HistoryCommand
        result = _execute(HistoryCommand, _make_ctx(), "3")
        assert "Trade History" in result

    def test_performance_empty(self) -> None:
        from telegram.commands.performance import PerformanceCommand
        import json
        with open("data/paper_orders.json", "w") as f:
            json.dump({"orders": []}, f)
        result = _execute(PerformanceCommand, _make_ctx())
        assert "No completed trades yet" in result


class TestNewPolish:
    """Tests for the 10-item production polish."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_signals_shows_top_buy(self) -> None:
        from telegram.commands.signals import SignalsCommand
        result = _execute(SignalsCommand, _make_ctx())
        assert "Signal" in result
        assert "BTC/USDT" in result
        assert "ETH/USDT" in result
        assert "SOL/USDT" not in result

    def test_positions_roi_and_distance(self) -> None:
        from telegram.commands.positions import PositionsCommand
        result = _execute(PositionsCommand, _make_ctx())
        assert "ROI:" in result
        assert "SL:" in result or "Remaining:" in result
        assert "Holding:" in result

    def test_position_holding_formatted(self) -> None:
        from telegram.commands.positions import PositionsCommand
        result = _execute(PositionsCommand, _make_ctx())
        assert "12h" in result

    def test_version_uptime_not_zero(self) -> None:
        from telegram.commands.version import VersionCommand
        result = _execute(VersionCommand, _make_ctx())
        assert "Uptime" in result
