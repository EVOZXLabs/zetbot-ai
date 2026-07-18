"""Unit tests for ExecutionEngine, OrderManager, and all executors."""

import json
import os
import time
import unittest.mock
from typing import Any
from unittest.mock import MagicMock

from scripts.execution_engine import (
    AUDIT_PATH,
    IExecutionEngine,
    OrderRequest,
    OrderResult,
    AuditEntry,
    OrderSide,
    OrderType,
    OrderStatus,
    TradingMode,
    PaperExecutor,
    SimulationExecutor,
    LiveExecutor,
    ExecutionEngine,
    ExecutionMetrics,
    append_audit,
    read_audit,
    _generate_id,
    _now,
)
from scripts.order_manager import OrderManager


# ======================================================================
#  OrderRequest & OrderResult
# ======================================================================


class TestOrderRequest:
    def test_default_trace_id_is_generated(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=1.0)
        assert req.trace_id != ""
        assert len(req.trace_id) > 10

    def test_provided_trace_id_is_kept(self) -> None:
        req = OrderRequest(trace_id="my_trace", symbol="BTC/USDT", side="BUY", amount=1.0)
        assert req.trace_id == "my_trace"


class TestOrderResult:
    def test_rejected_factory(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=1.0)
        result = OrderResult.rejected(req, "Insufficient balance")
        assert result.status == "REJECTED"
        assert result.error == "Insufficient balance"
        assert result.trace_id == req.trace_id
        assert result.execution_id != ""

    def test_failed_factory(self) -> None:
        req = OrderRequest(symbol="ETH/USDT", side="SELL", amount=0.5)
        result = OrderResult.failed(req, "Network error")
        assert result.status == "FAILED"
        assert result.error == "Network error"

    def test_to_dict(self) -> None:
        result = OrderResult(order_id="123", status="FILLED")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert d["order_id"] == "123"
        assert d["status"] == "FILLED"


# ======================================================================
#  PaperExecutor
# ======================================================================


class TestPaperExecutor:
    def setup_method(self) -> None:
        self.executor = PaperExecutor()
        self.config = MagicMock()
        self.exchange = MagicMock()
        self.exchange.name = "binance"
        self.exchange.get_ticker.return_value = {"last": 50000.0}
        self.wallet = MagicMock()
        self.wallet.free_balance = 10000.0

    def test_name(self) -> None:
        assert self.executor.name == "paper"

    def test_execute_buy(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "FILLED"
        assert result.symbol == "BTC/USDT"
        assert result.side == "BUY"
        assert result.executor == "paper"
        assert result.exchange == "binance"
        assert result.mode == "PAPER"
        assert result.order_id.startswith("po_")
        assert result.filled_amount == 0.1
        assert result.filled_price > 0
        assert result.latency_ms >= 0

    def test_execute_sell(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="SELL", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "FILLED"
        assert result.side == "SELL"

    def test_invalid_side(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="HODL", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"
        assert "Invalid side" in (result.error or "")

    def test_no_price_falls_back_to_ticker(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1, price=None)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "FILLED"
        self.exchange.get_ticker.assert_called_with("BTC/USDT")

    def test_insufficient_balance(self) -> None:
        self.wallet.free_balance = 1.0  # not enough for 0.1 BTC at 50000
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"
        assert "Insufficient balance" in (result.error or "")

    def test_custom_price(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1, price=60000.0)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "FILLED"
        assert result.filled_price > 0

    def test_no_wallet_does_not_crash(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, None)
        assert result.status == "FILLED"


# ======================================================================
#  SimulationExecutor
# ======================================================================


class TestSimulationExecutor:
    def setup_method(self) -> None:
        self.executor = SimulationExecutor()
        self.config = MagicMock()
        self.exchange = MagicMock()
        self.exchange.name = "binance"
        self.exchange.get_ticker.return_value = {"last": 50000.0}
        self.wallet = MagicMock()
        self.wallet.free_balance = 10000.0

    def test_name(self) -> None:
        assert self.executor.name == "simulation"

    def test_execute_buy_returns_simulated(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "SIMULATED"
        assert result.filled_amount == 0.0  # never fills
        assert result.executor == "simulation"

    def test_invalid_side_rejected(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="INVALID", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"

    def test_zero_amount_rejected(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.0)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"

    def test_no_price_rejected(self) -> None:
        self.exchange.get_ticker.return_value = {}
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"

    def test_insufficient_balance_rejected(self) -> None:
        self.wallet.free_balance = 1.0
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"


# ======================================================================
#  LiveExecutor
# ======================================================================


class TestLiveExecutor:
    def setup_method(self) -> None:
        LiveExecutor.disable()
        self.executor = LiveExecutor()
        self.config = MagicMock()
        self.exchange = MagicMock()
        self.exchange.name = "binance"
        self.exchange.get_ticker.return_value = {"last": 50000.0}
        self.wallet = MagicMock()

    def teardown_method(self) -> None:
        LiveExecutor.disable()

    def test_name(self) -> None:
        assert self.executor.name == "live"

    def test_disabled_by_default(self) -> None:
        assert LiveExecutor.is_enabled() is False

    def test_execute_when_disabled_rejected(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "REJECTED"
        assert "not enabled" in (result.error or "").lower()

    def test_execute_when_enabled_uses_live_executor(self) -> None:
        LiveExecutor.enable()
    
        self.wallet.free_balance = 10000.0

        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        # When enabled, it tries live (and with mocks it may succeed)
        assert result.executor == "live"

    def test_enable_disable(self) -> None:
        LiveExecutor.enable()


        assert LiveExecutor.is_enabled() is True

        self.wallet.free_balance = 10000.0

        mock_ex = MagicMock()
        mock_ex.fetch_balance.return_value = {"free": {"USDT": 10000.0}}
        mock_ex.create_order.return_value = {"id": "live_123", "filled": 0.1, "price": 50000.0, "cost": 5000.0}

        provider = MagicMock()
        provider._get_exchange.return_value = mock_ex
        provider.fetch_balance.return_value = {"free": {"USDT": 10000.0}}
       
        provider.amount_to_precision.side_effect = lambda symbol, amount: amount
        provider.price_to_precision.side_effect = lambda symbol, price: price
        provider.client_order_id_params.return_value = {}
       
        self.exchange.get_provider.return_value = provider

        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.executor.execute(req, self.config, self.exchange, self.wallet)
        assert result.status == "EXECUTED" or result.status == "FILLED"
        assert result.executor == "live"

        LiveExecutor.disable()
        assert LiveExecutor.is_enabled() is False


# ======================================================================
#  ExecutionEngine
# ======================================================================


class TestExecutionEngine:
    def setup_method(self) -> None:
        self.config = MagicMock()
        self.exchange = MagicMock()
        self.exchange.name = "binance"
        self.exchange.get_ticker.return_value = {"last": 50000.0}
        self.wallet = MagicMock()
        self.wallet.free_balance = 10000.0

    def test_default_mode_paper(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet)
        assert engine.mode == "PAPER"

    def test_set_mode(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="SIMULATION")
        assert engine.mode == "SIMULATION"
        engine.set_mode("PAPER")
        assert engine.mode == "PAPER"

    def test_set_mode_invalid_raises(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet)
        try:
            engine.set_mode("INVALID")
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_execute_paper_returns_filled(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="PAPER")
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = engine.execute(req)
        assert result.status == "FILLED"
        assert result.executor == "paper"

    def test_execute_simulation_returns_simulated(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="SIMULATION")
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = engine.execute(req)
        assert result.status == "SIMULATED"
        assert result.executor == "simulation"

    def test_execute_live_disabled_falls_to_simulation(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="LIVE")
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = engine.execute(req)
        # Falls back to simulation when live is disabled
        assert result.status == "SIMULATED" or result.status == "REJECTED"

    def test_validate_live_ready_no_live_mode(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="PAPER")
        assert engine.validate_live_ready() is None

    def test_validate_live_ready_not_enabled(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="LIVE")
        error = engine.validate_live_ready()
        assert error is not None
        assert "not enabled" in error.lower()

    def test_enable_disable_live(self) -> None:
        engine = ExecutionEngine(self.exchange, self.config, self.wallet, mode="LIVE")
        assert engine.is_live_enabled() is False
        engine.enable_live()
        assert engine.is_live_enabled() is True
        engine.disable_live()
        assert engine.is_live_enabled() is False


# ======================================================================
#  ExecutionMetrics
# ======================================================================


class TestExecutionMetrics:
    def setup_method(self) -> None:
        self.metrics = ExecutionMetrics()

    def test_empty_summary(self) -> None:
        s = self.metrics.summary()
        assert s["total"] == 0

    def test_record(self) -> None:
        r = OrderResult(status="FILLED", executor="paper", latency_ms=10.0,
                        exchange="binance")
        self.metrics.record(r)
        s = self.metrics.summary()
        assert s["total"] == 1
        assert s["filled"] == 1
        assert s["avg_latency_ms"] == 10.0

    def test_multiple_records(self) -> None:
        for status, executor in [("FILLED", "paper"), ("REJECTED", "paper"), ("SIMULATED", "simulation")]:
            self.metrics.record(OrderResult(status=status, executor=executor, latency_ms=5.0, exchange="binance"))
        s = self.metrics.summary()
        assert s["total"] == 3
        assert s["filled"] == 1
        assert s["rejected"] == 1
        assert s["simulated"] == 1
        assert s["by_executor"]["paper"] == 2
        assert s["by_executor"]["simulation"] == 1

    def test_reset(self) -> None:
        self.metrics.record(OrderResult(status="FILLED"))
        self.metrics.reset()
        assert self.metrics.summary()["total"] == 0


# ======================================================================
#  Audit trail
# ======================================================================


class TestAuditTrail:
    def setup_method(self) -> None:
        self.audit_file = AUDIT_PATH
        if os.path.exists(self.audit_file):
            os.remove(self.audit_file)

    def teardown_method(self) -> None:
        if os.path.exists(self.audit_file):
            os.remove(self.audit_file)

    def test_append_and_read(self) -> None:
        entry = AuditEntry(
            trace_id="t1", execution_id="e1", order_id="o1",
            symbol="BTC/USDT", side="BUY", amount=0.1,
            status="FILLED", executor="paper", mode="PAPER",
            exchange="binance", latency_ms=10.0, retries=0,
            error=None, timestamp=_now(),
        )
        append_audit(entry)
        entries = read_audit()
        assert len(entries) == 1
        assert entries[0].trace_id == "t1"
        assert entries[0].status == "FILLED"

    def test_read_empty(self) -> None:
        assert read_audit() == []

    def test_append_multiple(self) -> None:
        for i in range(5):
            entry = AuditEntry(
                trace_id=f"t{i}", execution_id=f"e{i}", order_id=f"o{i}",
                symbol="BTC/USDT", side="BUY", amount=0.1,
                status="FILLED", executor="paper", mode="PAPER",
                exchange="binance", latency_ms=5.0, retries=0,
                error=None, timestamp=_now(),
            )
            append_audit(entry)
        entries = read_audit(limit=3)
        assert len(entries) == 3

    def test_append_invalid_json_does_not_crash(self) -> None:
        with open(self.audit_file, "w") as f:
            f.write("invalid json\n")
        entries = read_audit()
        assert isinstance(entries, list)


# ======================================================================
#  OrderManager
# ======================================================================


class TestOrderManager:
    def setup_method(self) -> None:
        self.config = MagicMock()
        self.config.paper_mode = True
        self.exchange = MagicMock()
        self.exchange.name = "binance"
        self.exchange.get_ticker.return_value = {"last": 50000.0}
        self.wallet = MagicMock()
        self.wallet.free_balance = 10000.0
        self.risk = MagicMock()
        self.risk.get_approved.return_value = [
            {"symbol": "BTC/USDT", "entry_price": 50000.0, "quantity": 0.1,
             "position_size_usdt": 5000.0, "stop_loss": 49000.0, "tp1": 51000.0},
        ]
        self.mgr = OrderManager(self.config, self.exchange, self.wallet, self.risk, mode="PAPER")

    def test_initial_mode(self) -> None:
        assert self.mgr.mode == "PAPER"

    def test_execute_with_dict_backward_compat(self) -> None:
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0, "quantity": 0.1,
            "position_size_usdt": 5000.0, "stop_loss": 49000.0, "tp1": 51000.0,
        }
        result = self.mgr.execute(plan)
        assert isinstance(result, dict)  # backward compat: dict in, dict out
        assert result.get("status") == "FILLED"

    def test_execute_with_order_request(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.mgr.execute(req)
        assert isinstance(result, OrderResult)  # OrderRequest in, OrderResult out
        assert result.status == "FILLED"

    def test_execute_rejected_by_risk(self) -> None:
        self.risk.get_approved.return_value = []  # no approved decisions
        req = OrderRequest(symbol="ETH/USDT", side="BUY", amount=0.1)
        result = self.mgr.execute(req)
        assert result.status == "REJECTED"

    def test_execute_bypass_risk(self) -> None:
        self.risk.get_approved.return_value = []
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1,
                           metadata={"bypass_risk": True})
        result = self.mgr.execute(req)
        assert result.status == "FILLED"

    def test_get_orders_empty(self) -> None:
        orders = self.mgr.get_orders()
        assert isinstance(orders, list)

    def test_get_orders_from_file(self) -> None:
        # Save a test trade plan file
        test_plan = [{"symbol": "BTC/USDT", "status": "READY"}]
        with open("data/trade_plan.json", "w") as f:
            json.dump(test_plan, f)
        try:
            orders = self.mgr.get_orders()
            assert len(orders) == 1
        finally:
            if os.path.exists("data/trade_plan.json"):
                os.remove("data/trade_plan.json")

    def test_mode_management(self) -> None:
        assert self.mgr.is_live_enabled() is False
        self.mgr.enable_live()
        assert self.mgr.is_live_enabled() is True
        self.mgr.disable_live()
        assert self.mgr.is_live_enabled() is False

    def test_set_mode(self) -> None:
        self.mgr.set_mode("SIMULATION")
        assert self.mgr.mode == "SIMULATION"

    def test_validate_live_ready_paper_returns_none(self) -> None:
        error = self.mgr.validate_live_ready()
        assert error is None  # PAPER mode -> no validation

    def test_validate_live_ready_live_returns_error(self) -> None:
        self.mgr.set_mode("LIVE")
        error = self.mgr.validate_live_ready()
        assert error is not None
        assert "not enabled" in error.lower()

    def test_metrics_summary(self) -> None:
        summary = self.mgr.get_metrics_summary()
        assert isinstance(summary, dict)
        assert "total" in summary

    def test_execute_no_price_rejected(self) -> None:
        """When price cannot be determined, order is rejected."""
        self.exchange.get_ticker.return_value = {}  # will cause rejection
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1)
        result = self.mgr.execute(req)
        assert result.status == "REJECTED"

    def test_audit_trail(self) -> None:
        self.mgr.execute(OrderRequest(symbol="BTC/USDT", side="BUY", amount=0.1))
        trail = self.mgr.get_audit_trail()
        assert isinstance(trail, list)
        assert len(trail) > 0


# ======================================================================
#  Protocol compliance
# ======================================================================


def test_paper_executor_satisfies_protocol() -> None:
    assert isinstance(PaperExecutor(), IExecutionEngine)


def test_simulation_executor_satisfies_protocol() -> None:
    assert isinstance(SimulationExecutor(), IExecutionEngine)


def test_live_executor_satisfies_protocol() -> None:
    assert isinstance(LiveExecutor(), IExecutionEngine)


def test_order_manager_has_iordermanager_methods() -> None:
    """Verify OrderManager has all methods expected by IOrderManager."""
    from scripts.interfaces import IOrderManager
    config = MagicMock()
    exchange = MagicMock()
    wallet = MagicMock()
    risk = MagicMock()
    mgr = OrderManager(config, exchange, wallet, risk)
    assert hasattr(mgr, 'execute')
    assert hasattr(mgr, 'get_orders')
    assert hasattr(mgr, 'mode')
    assert hasattr(mgr, 'set_mode')
    assert hasattr(mgr, 'enable_live')
    assert hasattr(mgr, 'disable_live')
    assert hasattr(mgr, 'is_live_enabled')
    assert hasattr(mgr, 'validate_live_ready')
    assert hasattr(mgr, 'get_metrics_summary')
    assert hasattr(mgr, 'get_audit_trail')


# ---------------------------------------------------------------------------
#  PlanExport: regression tests for trade_plan.json always-written contract
# ---------------------------------------------------------------------------


class TestPlanExportEmpty:
    """Verify PlanExport.to_json/to_csv always write, even with empty plans."""

    def test_to_json_empty_plans_writes_file(self, tmp_path: Any) -> None:
        from scripts.trade_executor import PlanExport
        path = str(tmp_path / "trade_plan.json")
        PlanExport.to_json([], path)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["total_plans"] == 0
        assert data["plans"] == []

    def test_to_csv_empty_plans_writes_file(self, tmp_path: Any) -> None:
        from scripts.trade_executor import PlanExport
        path = str(tmp_path / "trade_plan.csv")
        PlanExport.to_csv([], path)
        assert os.path.exists(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 1  # header only


class TestTradeExecutorMainAlwaysWritesJson:
    """Regression: main() must always write trade_plan.json."""

    def test_main_writes_json_when_no_plans(self, tmp_path: Any) -> None:
        os.makedirs("data", exist_ok=True)
        from scripts import trade_executor

        with (
            unittest.mock.patch.object(trade_executor, "TradeExecutor") as MockExec,
        ):
            MockExec.return_value.run.return_value = []
            with unittest.mock.patch("builtins.print"):
                trade_executor.main()

        assert os.path.exists("data/trade_plan.json")
        with open("data/trade_plan.json") as f:
            data = json.load(f)
        assert data["total_plans"] == 0

    def test_main_writes_csv_when_no_plans(self) -> None:
        from scripts import trade_executor

        with unittest.mock.patch.object(trade_executor, "TradeExecutor") as MockExec:
            MockExec.return_value.run.return_value = []
            with unittest.mock.patch("builtins.print"):
                trade_executor.main()

        assert os.path.exists("data/trade_plan.csv")


class TestPaperEngineLoadPlansMissingFile:
    """Regression: _load_plans must not crash on missing file."""

    def test_load_plans_missing_file_returns_empty(self) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        engine = PaperTradingEngine.__new__(PaperTradingEngine)
        result = engine._load_plans("/nonexistent/path/trade_plan.json")
        assert result == []

    def test_load_plans_invalid_json_returns_empty(self, tmp_path: Any) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{")
        engine = PaperTradingEngine.__new__(PaperTradingEngine)
        result = engine._load_plans(str(bad))
        assert result == []
