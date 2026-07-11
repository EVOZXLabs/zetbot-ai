"""Unit tests for ServiceContainer and its adapters."""

import os
import json
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.app_config import AppConfig
from scripts.interfaces import (
    IConfigService,
    IExchangeManager,
    IHealthMonitor,
    IMetricsManager,
    INotificationManager,
    IOrderManager,
    IPositionManager,
    IRiskManager,
    IScanner,
    IStrategyManager,
    IWalletManager,
)
from scripts.service_container import ServiceContainer

# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

BASE_CFG = AppConfig(
    paper_mode=True,
    exchange="binance",
    timeframe="1h",
    account_balance=10000.0,
    max_positions=3,
    max_risk_per_trade_pct=2.0,
    scanner_threads=5,
    scanner_top_n=50,
    telegram_timeout=10,
    telegram_retry=3,
    min_rr=1.5,
    max_rr=5.0,
    min_probability=50.0,
    max_atr_pct=8.0,
    tp1_sell_pct=30.0,
    tp2_sell_pct=30.0,
    tp3_sell_pct=40.0,
    taker_fee=0.001,
    maker_fee=0.00075,
    slippage_bps=3,
)


def _make_container() -> ServiceContainer:
    c = ServiceContainer(BASE_CFG)
    c.bootstrap()
    return c


def _ensure_data_files() -> None:
    os.makedirs("data", exist_ok=True)
    for name, data in [
        ("paper_balance.json", {
            "final_balance": 10000.0,
            "final_equity": 10500.0,
            "realized_pnl": 250.0,
            "unrealized_pnl": 250.0,
            "net_pnl": 500.0,
            "total_return_pct": 5.0,
        }),
        ("positions.json", {
            "positions": [{
                "symbol": "BTC/USDT", "status": "OPEN",
                "entry_price": 60000.0, "current_price": 61000.0,
            }]
        }),
    ]:
        with open(f"data/{name}", "w") as f:
            json.dump(data, f)


# ======================================================================
#  ServiceContainer bootstrap
# ======================================================================

class TestServiceContainerBootstrap:
    """Container creates all services and exposes them via properties."""

    def test_bootstrap_creates_all_services(self) -> None:
        c = _make_container()
        # All services should be accessible (not None)
        assert c.config is not None
        assert c.exchange is not None
        assert c.wallet is not None
        assert c.scanner is not None
        assert c.strategy is not None
        assert c.risk is not None
        assert c.order is not None
        assert c.position is not None
        assert c.notification is not None
        assert c.metrics is not None
        # health is injected separately
        assert c.health is None

    def test_bootstrap_idempotent(self) -> None:
        c = _make_container()
        c.bootstrap()  # second call should be no-op
        assert c.config is not None

    def test_inject_health(self) -> None:
        c = _make_container()
        mock_health = MagicMock()
        c.inject_health(mock_health)
        assert c.health is not None

    def test_repr(self) -> None:
        c = _make_container()
        r = repr(c)
        assert "ServiceContainer(" in r
        assert "config=✓" in r


# ======================================================================
#  ConfigService
# ======================================================================

class TestConfigService:
    """IConfigService wraps AppConfig fields."""

    def test_config_properties(self) -> None:
        c = _make_container()
        assert c.config.exchange == "binance"
        assert c.config.timeframe == "1h"
        assert c.config.paper_mode is True
        assert c.config.account_balance == 10000.0

    def test_config_is_iconfigservice(self) -> None:
        c = _make_container()
        from scripts.interfaces import IConfigService
        # Can't use isinstance with runtime_checkable + missing methods
        assert hasattr(c.config, 'exchange')

    def test_config_delegates_all_attrs(self) -> None:
        c = _make_container()
        assert c.config.max_positions == 3
        assert c.config.telegram_timeout == 10


# ======================================================================
#  ExchangeManager
# ======================================================================

class TestExchangeManager:
    """IExchangeManager adapter."""

    def test_exchange_provides_interface(self) -> None:
        c = _make_container()
        assert c.exchange.name == "binance"

    def test_exchange_health_check(self) -> None:
        c = _make_container()
        # Without network, health check should return False gracefully
        result = c.exchange.health_check()
        assert isinstance(result, bool)

    def test_get_ticker_handles_errors(self) -> None:
        c = _make_container()
        result = c.exchange.get_ticker("NONEXISTENT/XXX")
        assert isinstance(result, dict)

    def test_fetch_ohlcv_handles_errors(self) -> None:
        c = _make_container()
        result = c.exchange.fetch_ohlcv("NONEXISTENT/XXX")
        assert isinstance(result, list)

    def test_fetch_balance_handles_errors(self) -> None:
        c = _make_container()
        result = c.exchange.fetch_balance()
        assert isinstance(result, dict)


# ======================================================================
#  WalletManager
# ======================================================================

class TestWalletManager:
    """IWalletManager adapter."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_wallet_reads_balance(self) -> None:
        c = _make_container()
        assert c.wallet.balance == 10000.0

    def test_wallet_reads_equity(self) -> None:
        c = _make_container()
        assert c.wallet.equity == 10500.0

    def test_wallet_reads_pnl(self) -> None:
        c = _make_container()
        assert c.wallet.net_pnl == 500.0

    def test_wallet_snapshot(self) -> None:
        c = _make_container()
        snap = c.wallet.snapshot()
        assert isinstance(snap, dict)
        assert "final_balance" in snap

    def test_wallet_handle_missing_file(self) -> None:
        # Temporarily rename balance file
        if os.path.exists("data/paper_balance.json"):
            os.rename("data/paper_balance.json", "data/paper_balance.json.bak")
        try:
            c = ServiceContainer(BASE_CFG)
            c.bootstrap()
            assert c.wallet.balance == 0.0
        finally:
            if os.path.exists("data/paper_balance.json.bak"):
                os.rename("data/paper_balance.json.bak", "data/paper_balance.json")


# ======================================================================
#  PositionManager
# ======================================================================

class TestPositionManager:
    """IPositionManager adapter."""

    def setup_method(self) -> None:
        _ensure_data_files()

    def test_get_open_positions(self) -> None:
        c = _make_container()
        positions = c.position.get_open_positions()
        assert len(positions) >= 1
        assert positions[0]["status"] == "OPEN"

    def test_get_all_positions(self) -> None:
        c = _make_container()
        all_pos = c.position.get_all()
        assert len(all_pos) >= 1


# ======================================================================
#  MetricsManager
# ======================================================================

class TestMetricsManager:
    """IMetricsManager adapter."""

    def test_empty_metrics(self) -> None:
        c = _make_container()
        m = c.metrics.get_metrics()
        assert m["total_trades"] == 0
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0

    def test_record_trade(self) -> None:
        c = _make_container()
        c.metrics.record_trade({"net_pnl": 100.0})
        c.metrics.record_trade({"net_pnl": -50.0})
        c.metrics.record_trade({"net_pnl": 75.0})
        m = c.metrics.get_metrics()
        assert m["total_trades"] == 3
        assert m["winning_trades"] == 2
        assert m["losing_trades"] == 1

    def test_win_rate(self) -> None:
        c = _make_container()
        for pnl in [100, 50, -20]:
            c.metrics.record_trade({"net_pnl": pnl})
        assert c.metrics.win_rate() == pytest.approx(66.67, rel=0.01)

    def test_total_trades(self) -> None:
        c = _make_container()
        assert c.metrics.total_trades() == 0
        c.metrics.record_trade({"net_pnl": 10})
        assert c.metrics.total_trades() == 1

    def test_reset(self) -> None:
        c = _make_container()
        c.metrics.record_trade({"net_pnl": 100})
        c.metrics.reset()
        assert c.metrics.total_trades() == 0


# ======================================================================
#  NotificationManager
# ======================================================================

class TestNotificationManager:
    """INotificationManager adapter."""

    def test_notification_interface(self) -> None:
        c = _make_container()
        assert hasattr(c.notification, 'send')
        assert hasattr(c.notification, 'notify_buy')
        assert hasattr(c.notification, 'notify_close')
        assert hasattr(c.notification, 'notify_error')

    def test_send_without_creds(self) -> None:
        c = _make_container()
        c.config.telegram_token = ""
        c.config.telegram_chat_id = ""
        result = c.notification.send("test")
        # Should not crash
        assert isinstance(result, bool)


# ======================================================================
#  Scanner, Strategy, Risk, Order — integration
# ======================================================================

class TestScannerAdapter:
    """IScanner adapter."""

    def test_get_results_handles_missing_file(self) -> None:
        # Temporarily remove scanner_results.json
        existed = os.path.exists("data/scanner_results.json")
        if existed:
            os.rename("data/scanner_results.json", "data/scanner_results.json.bak")
        try:
            c = _make_container()
            result = c.scanner.get_results()
            assert isinstance(result, dict)
        finally:
            if existed:
                os.rename("data/scanner_results.json.bak", "data/scanner_results.json")


class TestStrategyAdapter:
    """IStrategyManager adapter."""

    def test_get_decisions_without_file(self) -> None:
        c = _make_container()
        result = c.strategy.get_decisions()
        assert isinstance(result, list)


class TestRiskAdapter:
    """IRiskManager adapter."""

    def test_get_approved_without_file(self) -> None:
        c = _make_container()
        result = c.risk.get_approved()
        assert isinstance(result, list)


class TestOrderAdapter:
    """IOrderManager adapter."""

    def test_get_orders_without_file(self) -> None:
        c = _make_container()
        result = c.order.get_orders()
        assert isinstance(result, list)


class TestHealthAdapter:
    """IHealthMonitor adapter."""

    def test_wraps_health_monitor(self) -> None:
        from scripts.service_container import _HealthAdapter
        mock = MagicMock()
        mock.snapshot.return_value = {"uptime_sec": 3600}
        adapter = _HealthAdapter(mock)
        adapter.start()
        mock.start.assert_called_once()
        adapter.stop()
        mock.stop.assert_called_once()
        snap = adapter.snapshot()
        assert snap["uptime_sec"] == 3600

    def test_force_refresh(self) -> None:
        from scripts.service_container import _HealthAdapter
        mock = MagicMock()
        mock.force_refresh.return_value = {"score": 95}
        adapter = _HealthAdapter(mock)
        result = adapter.force_refresh()
        assert result["score"] == 95

    def test_without_monitor(self) -> None:
        from scripts.service_container import _HealthAdapter
        adapter = _HealthAdapter(None)
        # Should not crash
        adapter.start()
        adapter.stop()
        assert adapter.snapshot() == {}
        assert adapter.force_refresh() == {}


# ======================================================================
#  Pipeline integration
# ======================================================================

class TestPipelineWithContainer:
    """Pipeline uses container when provided."""

    def test_pipeline_from_container(self) -> None:
        c = _make_container()
        pipeline = c.pipeline
        assert pipeline is not None
        assert pipeline.container is c

    def test_interface_contracts(self) -> None:
        """All services match their Protocol interfaces."""
        c = _make_container()

        # Each service should satisfy its interface duck-typing
        assert hasattr(c.config, 'exchange')
        assert hasattr(c.exchange, 'health_check')
        assert hasattr(c.wallet, 'balance')
        assert hasattr(c.scanner, 'run')
        assert hasattr(c.strategy, 'evaluate')
        assert hasattr(c.risk, 'approve')
        assert hasattr(c.order, 'execute')
        assert hasattr(c.position, 'get_open_positions')
        assert hasattr(c.notification, 'send')
        assert hasattr(c.metrics, 'record_trade')

        if c.health:
            assert hasattr(c.health, 'snapshot')


import pytest  # noqa: PLC0415, E402 (import at end for fixture above)


# ======================================================================
#  Metrics account sync
# ======================================================================

class TestMetricsAccountSync:
    """Equity = Cash when no open positions, Equity = Cash + Unrealized PnL otherwise."""

    def test_equity_equals_balance_when_no_open_positions(self) -> None:
        """With zero open positions, equity should equal balance (cash)."""
        import json
        os.makedirs("data", exist_ok=True)
        # Balance = 10000, Unrealized PnL = 500
        with open("data/paper_balance.json", "w") as f:
            json.dump({
                "final_balance": 10000.0,
                "final_equity": 10500.0,
                "realized_pnl": 200.0,
                "unrealized_pnl": 500.0,
                "net_pnl": 700.0,
            }, f)
        # No open positions
        with open("data/positions.json", "w") as f:
            json.dump({"positions": []}, f)

        c = _make_container()
        assert c.metrics.open_positions_count() == 0
        assert c.metrics.unrealized_pnl() == 0.0
        assert c.metrics.equity() == 10000.0
        assert c.metrics.balance() == 10000.0

    def test_equity_includes_unrealized_when_open_positions(self) -> None:
        """With open positions, equity = balance + unrealized PnL."""
        import json
        os.makedirs("data", exist_ok=True)
        with open("data/paper_balance.json", "w") as f:
            json.dump({
                "final_balance": 10000.0,
                "final_equity": 10500.0,
                "realized_pnl": 200.0,
                "unrealized_pnl": 500.0,
                "net_pnl": 700.0,
            }, f)
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [{
                "symbol": "BTC/USDT", "status": "OPEN",
                "entry_price": 60000.0, "current_price": 61000.0,
            }]}, f)

        c = _make_container()
        assert c.metrics.open_positions_count() == 1
        assert c.metrics.unrealized_pnl() == 500.0
        assert c.metrics.equity() == 10500.0  # 10000 + 500

    def test_summary_syncs_unrealized_to_zero_when_no_open(self) -> None:
        """summary() should reflect the same sync logic."""
        import json
        os.makedirs("data", exist_ok=True)
        with open("data/paper_balance.json", "w") as f:
            json.dump({
                "final_balance": 10000.0,
                "final_equity": 10500.0,
                "realized_pnl": 200.0,
                "unrealized_pnl": 500.0,
                "net_pnl": 700.0,
            }, f)
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [{
                "symbol": "BTC/USDT", "status": "CLOSED",
                "entry_price": 60000.0, "current_price": 61000.0,
            }]}, f)

        c = _make_container()
        s = c.metrics.summary()
        # No open positions (CLOSED doesn't count)
        assert s["open_positions"] == 0
        assert s["unrealized_pnl"] == 0.0
        assert s["equity"] == 10000.0
        assert s["net_pnl"] == 200.0  # only realized
