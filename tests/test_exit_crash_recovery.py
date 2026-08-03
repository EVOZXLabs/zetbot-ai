"""Regression tests for BUG-1 crash-window exits and BUG-4 exit statuses.

Covers:
- Write-ahead: the anticipated post-exit state lands in positions.json
  BEFORE the market sell is submitted, so a crash between fill and
  persist can never make a restart re-sell the same quantity
- Stale-snapshot guard: a reconciler holding a stale position dict cannot
  double-sell a level a concurrent reconciler already persisted
- Rollback: a failed sell restores the pre-exit state
- BUG-4: stop-loss exits keep their STOPPED status (never relabeled
  "Take Profit")
- Paper-engine restart recovery: tp1_sold flags survive save/load and
  block re-selling on the next cycle

These tests never touch a real exchange; a recording fake stands in for
CCXT. All data files are redirected to a throwaway directory.
"""

import os
from typing import Any

import pytest

from scripts.execution_engine import LiveExecutor
from scripts.execution_pipeline import ExecutionPipeline
from scripts.execution_provider import (
    LiveExecutionProvider,
    OrderRequest,
    create_execution_provider,
)
from scripts.exit_gate import load_position
from scripts.paper_trading_engine import (
    PaperTradingEngine,
    VirtualPosition,
)


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
def _sandbox_data(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in a throwaway directory (never touch data/)."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)


@pytest.fixture(autouse=True)
def _reset_live_flag() -> None:
    """LiveExecutor.ENABLED is process-global — always restore it."""
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


def _make_pipeline() -> tuple[ExecutionPipeline, _RecordingExchange]:
    exchange = _RecordingExchange()
    manager = _FakeExchangeManager(_FakeProvider(exchange))
    provider = create_execution_provider("LIVE", manager, _FakeConfig())
    return ExecutionPipeline(provider), exchange


def _open_position(**overrides: Any) -> dict[str, Any]:
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
        "stop_loss": 95.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "realized_pnl": 0.0,
    }
    position.update(overrides)
    return position


class TestCrashWindowWriteAhead:
    """The crash window between order fill and caller persist is closed."""

    def test_tp_write_ahead_lands_in_positions_json_before_sell(self) -> None:
        pipeline, exchange = _make_pipeline()
        LiveExecutor.enable()
        try:
            pipeline.reconcile_position("BTC/USDT", 105.0, _open_position())
        finally:
            LiveExecutor.disable()

        assert len(exchange.orders) == 1
        # The file ALREADY shows the level as sold even though no caller
        # persisted anything — this is exactly the restart view after a
        # crash following the fill.
        persisted = load_position("BTC/USDT")
        assert persisted is not None
        assert persisted["tp1_hit"] is True
        assert persisted["remaining_qty"] == pytest.approx(0.07)

    def test_sl_write_ahead_lands_in_positions_json_before_sell(self) -> None:
        pipeline, exchange = _make_pipeline()
        LiveExecutor.enable()
        try:
            result = pipeline.reconcile_position(
                "BTC/USDT", 90.0, _open_position(tp1=0.0, stop_loss=95.0),
            )
        finally:
            LiveExecutor.disable()

        assert len(exchange.orders) == 1
        assert result is not None
        # BUG-4: stop-loss exits keep STOPPED, never "Take Profit".
        assert result["status"] == "STOPPED"
        persisted = load_position("BTC/USDT")
        assert persisted is not None
        assert persisted["status"] == "STOPPED"
        assert persisted["remaining_qty"] == pytest.approx(0.0)

    def test_failed_sell_rolls_back_write_ahead(self) -> None:
        pipeline, exchange = _make_pipeline()
        # Not armed -> the sell is rejected, state must be restored.
        result = pipeline.reconcile_position("BTC/USDT", 105.0, _open_position())

        assert exchange.orders == []
        assert result is not None
        assert result["status"] == "OPEN"
        assert result["tp1_hit"] is False
        persisted = load_position("BTC/USDT")
        assert persisted is not None
        assert persisted["tp1_hit"] is False
        assert persisted["remaining_qty"] == pytest.approx(0.1)


class TestStaleSnapshotGuard:
    """Two concurrent reconcilers can never both sell the same level."""

    def test_stale_snapshot_cannot_double_sell_already_persisted_level(
        self,
    ) -> None:
        import json

        # A concurrent reconciler already sold tp1 and persisted it.
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [
                _open_position(tp1_hit=True, remaining_qty=0.07),
            ]}, f)

        pipeline, exchange = _make_pipeline()
        # Armed: without the re-read guard this WOULD place a second sell.
        LiveExecutor.enable()
        try:
            # Caller passes a STALE snapshot taken before the other
            # reconciler's update (tp1_hit still False, remaining still
            # full).
            stale = _open_position()
            updated = pipeline.reconcile_position("BTC/USDT", 105.0, stale)
        finally:
            LiveExecutor.disable()

        assert exchange.orders == []
        assert updated is not None
        assert updated["remaining_qty"] == pytest.approx(0.1)
        persisted = load_position("BTC/USDT")
        assert persisted["tp1_hit"] is True
        assert persisted["remaining_qty"] == pytest.approx(0.07)


class TestPaperEngineRestartRecovery:
    """tp1_sold flags survive save/load and block re-selling after restart."""

    def _vp(self, tp1_sold: bool) -> VirtualPosition:
        return VirtualPosition(
            symbol="BTC/USDT",
            order_id="o1",
            quantity=1.0,
            remaining_qty=0.7,
            entry_price=100.0,
            current_price=105.0,
            unrealized_pnl=0.0,
            realized_pnl=15.0,
            total_pnl=15.0,
            cost_basis=100.0,
            status="OPEN",
            tp1=100.0,
            tp1_sold=tp1_sold,
        )

    def test_already_sold_level_is_not_resold_after_restart(self) -> None:
        engine = PaperTradingEngine(initial_balance=10000.0)
        engine.positions["BTC/USDT"] = self._vp(tp1_sold=True)
        engine._save_state()

        # Simulate a restart: a fresh engine loads the persisted state and
        # reconciles against a plan whose tp1 is marked hit.
        restarted = PaperTradingEngine(initial_balance=10000.0)
        assert restarted.positions["BTC/USDT"].tp1_sold is True
        restarted._reconcile(
            {"symbol": "BTC/USDT"},
            {"tp1_hit": True, "tp1": 100.0, "current_price": 105.0,
             "stop_loss": 0.0, "status": "OPEN"},
        )
        assert not any(o.side == "SELL" for o in restarted.orders)
        assert restarted.wallet.balance == pytest.approx(10000.0)

    def test_unsold_level_is_sold_after_restart(self) -> None:
        engine = PaperTradingEngine(initial_balance=10000.0)
        engine.positions["BTC/USDT"] = self._vp(tp1_sold=False)
        engine._save_state()

        restarted = PaperTradingEngine(initial_balance=10000.0)
        restarted._reconcile(
            {"symbol": "BTC/USDT"},
            {"tp1_hit": True, "tp1": 100.0, "current_price": 105.0,
             "stop_loss": 0.0, "status": "OPEN"},
        )
        sells = [o for o in restarted.orders if o.side == "SELL"]
        assert len(sells) == 1
        assert sells[0].quantity == pytest.approx(0.3)
        assert restarted.wallet.balance > 10000.0
