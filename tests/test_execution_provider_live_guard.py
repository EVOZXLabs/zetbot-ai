"""Regression tests for BUG-1: no live order path may bypass the arm switch.

LiveExecutionProvider is the only object that can submit real exchange
orders in the unified pipeline path (``_run_paper_di``) and the position
monitor (``_monitor_positions``). It must refuse to submit while live
trading is not armed — using the SAME in-memory flag that gates
ExecutionEngine's LiveExecutor — so that setting ``PAPER_MODE=false``
alone can never place a real order.

These tests never touch a real exchange; a recording fake stands in for
CCXT.
"""

from typing import Any

import pytest

from scripts.execution_provider import (
    LiveExecutionProvider,
    OrderRequest,
    create_execution_provider,
)
from scripts.execution_engine import LiveExecutor


class _RecordingExchange:
    """Fake CCXT-style exchange that records every create_order call."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        return {
            "id": "test_order_1",
            "status": "closed",
            "filled": kwargs["amount"],
            "average": 100.0,
            "price": 100.0,
            "cost": kwargs["amount"] * 100.0,
            "fee": {"cost": 0.0},
        }


class _FakeProvider:
    def __init__(self, exchange: _RecordingExchange) -> None:
        self._ex = exchange

    def _get_exchange(self) -> _RecordingExchange:
        return self._ex

    def fetch_balance(self) -> dict[str, Any]:
        return {"free": {"USDT": 100000.0}}

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": 100.0, "ask": 100.0, "bid": 100.0}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        return price

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        return {}

    def market_buy_requires_price(self) -> bool:
        return False


class _FakeExchangeManager:
    name = "binance"

    def __init__(self, provider: _FakeProvider) -> None:
        self._provider = provider

    def get_provider(self) -> _FakeProvider:
        return self._provider


class _FakeConfig:
    quote_currency = "USDT"
    exchange = "binance"


@pytest.fixture(autouse=True)
def _reset_live_flag() -> None:
    """LiveExecutor.ENABLED is process-global — always restore it."""
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


def _make_provider() -> tuple[LiveExecutionProvider, _RecordingExchange]:
    exchange = _RecordingExchange()
    manager = _FakeExchangeManager(_FakeProvider(exchange))
    return LiveExecutionProvider(manager, _FakeConfig()), exchange


def _request(side: str = "BUY") -> OrderRequest:
    return OrderRequest(
        symbol="BTC/USDT", side=side, type="MARKET",
        amount=0.1, price=100.0,
    )


class TestLiveProviderArmGuard:
    def test_buy_rejected_when_not_armed_and_no_exchange_call(self) -> None:
        provider, exchange = _make_provider()
        result = provider.execute_buy(_request("BUY"))
        assert result.status == "REJECTED"
        assert "not armed" in (result.error or "").lower()
        assert result.executor == "live"
        assert exchange.orders == []

    def test_sell_rejected_when_not_armed_and_no_exchange_call(self) -> None:
        provider, exchange = _make_provider()
        result = provider.execute_sell(_request("SELL"))
        assert result.status == "REJECTED"
        assert "not armed" in (result.error or "").lower()
        assert result.executor == "live"
        assert exchange.orders == []

    def test_buy_submitted_when_armed(self) -> None:
        provider, exchange = _make_provider()
        LiveExecutor.enable()
        try:
            result = provider.execute_buy(_request("BUY"))
            assert result.status == "FILLED"
            assert len(exchange.orders) == 1
            assert exchange.orders[0]["side"] == "buy"
        finally:
            LiveExecutor.disable()

    def test_sell_submitted_when_armed(self) -> None:
        provider, exchange = _make_provider()
        LiveExecutor.enable()
        try:
            result = provider.execute_sell(_request("SELL"))
            assert result.status == "FILLED"
            assert len(exchange.orders) == 1
            assert exchange.orders[0]["side"] == "sell"
        finally:
            LiveExecutor.disable()

    def test_disarm_mid_session_blocks_new_orders(self) -> None:
        provider, exchange = _make_provider()
        LiveExecutor.enable()
        try:
            assert provider.execute_buy(_request()).status == "FILLED"
            assert len(exchange.orders) == 1
        finally:
            LiveExecutor.disable()
        # After disarm, the same provider instance must refuse again.
        result = provider.execute_buy(_request())
        assert result.status == "REJECTED"
        assert len(exchange.orders) == 1  # no additional exchange call

    def test_guard_matches_execution_engine_flag(self) -> None:
        """The provider must agree with ExecutionEngine on the arm flag."""
        from scripts.execution_engine import ExecutionEngine
        from unittest.mock import MagicMock

        engine = ExecutionEngine(MagicMock(), _FakeConfig(), MagicMock(), mode="LIVE")
        assert engine.is_live_enabled() is False

        provider, exchange = _make_provider()
        assert provider.execute_buy(_request()).status == "REJECTED"
        assert exchange.orders == []

        engine.enable_live()
        try:
            assert engine.is_live_enabled() is True
            assert provider.execute_buy(_request()).status == "FILLED"
            assert len(exchange.orders) == 1
        finally:
            engine.disable_live()

    def test_create_execution_provider_live_respects_guard(self) -> None:
        exchange = _RecordingExchange()
        manager = _FakeExchangeManager(_FakeProvider(exchange))
        provider = create_execution_provider("LIVE", manager, _FakeConfig())
        # NOTE: no isinstance() here — tests/test_account_balance_consistency.py
        # calls importlib.reload() on scripts.execution_provider, which
        # rebinds the classes mid-run and breaks class-identity checks.
        assert provider.name == "live"
        assert provider.mode == "LIVE"
        result = provider.execute_buy(_request())
        assert result.status == "REJECTED"
        assert exchange.orders == []


class TestPipelinePathArmGuard:
    """BUY and TP/SL exits through ExecutionPipeline must be blocked."""

    def test_execute_plan_buy_blocked_when_not_armed(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        provider, exchange = _make_provider()
        pipeline = ExecutionPipeline(provider)
        plan = {
            "symbol": "BTC/USDT",
            "entry_price": 100.0,
            "quantity": 0.1,
            "position_size_usdt": 10.0,
            "stop_loss": 90.0,
            "tp1": 110.0,
        }
        result = pipeline.execute_plan(plan)
        assert result is not None
        assert result.status == "REJECTED"
        assert "not armed" in (result.error or "").lower()
        assert exchange.orders == []

    def test_execute_plan_buy_goes_through_when_armed(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        provider, exchange = _make_provider()
        pipeline = ExecutionPipeline(provider)
        plan = {
            "symbol": "BTC/USDT",
            "entry_price": 100.0,
            "quantity": 0.1,
            "position_size_usdt": 10.0,
            "stop_loss": 90.0,
            "tp1": 110.0,
        }
        LiveExecutor.enable()
        try:
            result = pipeline.execute_plan(plan)
            assert result.status == "FILLED"
            assert len(exchange.orders) == 1
            assert exchange.orders[0]["side"] == "buy"
        finally:
            LiveExecutor.disable()

    def test_tp_exit_blocked_when_not_armed(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        provider, exchange = _make_provider()
        pipeline = ExecutionPipeline(provider)
        position = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 100.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "cost_basis": 10.0,
            "tp1": 100.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "stop_loss": 0.0,
            "tp1_hit": False,
            "realized_pnl": 0.0,
        }
        updated = pipeline.reconcile_position("BTC/USDT", 105.0, position)
        assert updated is not None
        # TP triggered but the exit could not fill -> position stays open.
        assert updated.get("status") == "OPEN"
        assert updated.get("remaining_qty") == 0.1
        assert exchange.orders == []

    def test_tp_exit_goes_through_when_armed(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        provider, exchange = _make_provider()
        pipeline = ExecutionPipeline(provider)
        position = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 100.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "cost_basis": 10.0,
            "tp1": 100.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "stop_loss": 0.0,
            "tp1_hit": False,
            "realized_pnl": 0.0,
        }
        LiveExecutor.enable()
        try:
            updated = pipeline.reconcile_position("BTC/USDT", 105.0, position)
            assert len(exchange.orders) == 1
            assert exchange.orders[0]["side"] == "sell"
            assert updated.get("tp1_hit") is True
            assert updated.get("remaining_qty") == pytest.approx(0.07)
        finally:
            LiveExecutor.disable()

    def test_sl_exit_blocked_when_not_armed(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        provider, exchange = _make_provider()
        pipeline = ExecutionPipeline(provider)
        position = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 100.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "cost_basis": 10.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "stop_loss": 95.0,
            "tp1_hit": False,
            "realized_pnl": 0.0,
        }
        updated = pipeline.reconcile_position("BTC/USDT", 90.0, position)
        assert updated is not None
        assert updated.get("status") == "OPEN"
        assert updated.get("remaining_qty") == 0.1
        assert exchange.orders == []
