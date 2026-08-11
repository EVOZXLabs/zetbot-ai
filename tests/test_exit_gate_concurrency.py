"""Regression tests for BUG-2: no duplicate/oversell TP-SL exits in LIVE mode.

Root cause (see the BUG-2 audit): in LIVE mode several independent
threads drive TP/SL exits — the position monitor (main.py, ~60 s), the
pipeline reconciliation (pipeline.py, ~300 s), the protection scheduler
(~8 s) and Telegram /sell. Each used to read ``data/positions.json``
independently, decide "TP/SL hit" and submit a MARKET sell. Two
concurrent readers could both sell the same quantity (oversell),
because the only idempotency guard (``tp1_hit`` / ``remaining_qty``)
was persisted only AFTER the sell.

Fix: ``scripts.exit_gate`` provides ONE per-symbol lock shared by every
exit path plus an atomic per-symbol ``positions.json`` read-modify-write,
so the read -> decide -> sell -> persist cycle is serialized and every
decision is made against the freshest authoritative state.

These tests never touch a real exchange; a recording fake stands in for
CCXT, and ``exit_gate.POSITIONS_PATH`` is redirected to a tmp file.
"""

from threading import Barrier, Event, Thread
from typing import Any

import pytest

from scripts.execution_engine import LiveExecutor
from scripts.execution_pipeline import ExecutionPipeline
from scripts.execution_provider import LiveExecutionProvider
import scripts.execution_provider as execution_provider
import scripts.exit_gate as exit_gate

SYMBOL = "BTC/USDT"


class _RecordingExchange:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self._lock_guard = __import__("threading").Lock()

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock_guard:
            self.orders.append(kwargs)
        return {
            "id": f"repro{len(self.orders)}",
            "status": "closed",
            "filled": kwargs["amount"],
            "average": float(kwargs.get("price") or 0) or 100.0,
            "price": kwargs.get("price") or 100.0,
            "cost": float(kwargs["amount"]) * 100.0,
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
def _isolate(tmp_path, monkeypatch) -> None:
    """Redirect exit_gate's positions.json and the pipeline event log to
    tmp files, and reset the process-global arm flag."""
    monkeypatch.setattr(exit_gate, "POSITIONS_PATH", str(tmp_path / "positions.json"))
    monkeypatch.setattr(execution_provider, "EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


def _base_position(**overrides: Any) -> dict[str, Any]:
    pos = {
        "symbol": SYMBOL,
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
        "realized_pnl": 0.0,
    }
    pos.update(overrides)
    return pos


def _seed_position(position: dict[str, Any]) -> None:
    exit_gate.save_position(position["symbol"], position)


def _make_pipeline(exchange: _RecordingExchange) -> ExecutionPipeline:
    manager = _FakeExchangeManager(_FakeProvider(exchange))
    provider = LiveExecutionProvider(manager, _FakeConfig())
    return ExecutionPipeline(provider)


def _sell_orders(exchange: _RecordingExchange) -> list[dict[str, Any]]:
    return [o for o in exchange.orders if o.get("side") == "sell"]


def _run_two_actors(actor_a, actor_b, barrier: Barrier) -> None:
    start_a, start_b = Event(), Event()

    def _wrap(actor: Any, start: Event) -> None:
        start.set()
        barrier.wait(timeout=15)
        actor()

    t1 = Thread(target=_wrap, args=(actor_a, start_a))
    t2 = Thread(target=_wrap, args=(actor_b, start_b))
    t1.start()
    t2.start()
    start_a.wait(timeout=5)
    start_b.wait(timeout=5)
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not t1.is_alive(), "actor A did not finish"
    assert not t2.is_alive(), "actor B did not finish"


class TestConcurrentTp1:
    def test_concurrent_tp1_submits_exactly_one_sell(self) -> None:
        """Two threads reconcile the same fresh position at price >= TP1:
        only ONE market sell may leave (the second sees tp1_hit already
        persisted by the first, inside the same per-symbol lock)."""
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)
        _seed_position(_base_position())
        barrier = Barrier(2)
        LiveExecutor.enable()
        try:
            _run_two_actors(
                lambda: exit_gate.reconcile_exit(pipeline, SYMBOL, 105.0, {}, cancel_protection=None),
                lambda: exit_gate.reconcile_exit(pipeline, SYMBOL, 105.0, {}, cancel_protection=None),
                barrier,
            )
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 1, f"expected exactly one TP1 sell, got {len(sells)}"
        assert sells[0]["amount"] == pytest.approx(0.03)

        final = exit_gate.load_position(SYMBOL)
        assert final is not None
        assert final["tp1_hit"] is True
        assert final["remaining_qty"] == pytest.approx(0.07)


class TestConcurrentSl:
    def test_concurrent_sl_submits_exactly_one_sell(self) -> None:
        """Two threads reconcile the same position at price <= SL: only
        ONE full-close market sell may leave (the second sees the
        position already CLOSED)."""
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)
        _seed_position(_base_position())
        barrier = Barrier(2)
        LiveExecutor.enable()
        try:
            _run_two_actors(
                lambda: exit_gate.reconcile_exit(pipeline, SYMBOL, 90.0, {}, cancel_protection=None),
                lambda: exit_gate.reconcile_exit(pipeline, SYMBOL, 90.0, {}, cancel_protection=None),
                barrier,
            )
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 1, f"expected exactly one SL sell, got {len(sells)}"
        assert sells[0]["amount"] == pytest.approx(0.1)

        final = exit_gate.load_position(SYMBOL)
        assert final is not None
        assert final["status"] in ("CLOSED", "STOPPED")
        assert final["remaining_qty"] == pytest.approx(0.0)


class TestMonitorVsPipeline:
    def test_monitor_and_pipeline_submit_exactly_one_sell(self) -> None:
        """The monitor (reconcile_exit with on_reconciled) and the
        pipeline (reconcile_exit with cancel_protection) run concurrently
        on the same symbol — the shared per-symbol lock allows only ONE
        market sell."""
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)
        _seed_position(_base_position())
        barrier = Barrier(2)
        closures: list[dict[str, Any]] = []
        LiveExecutor.enable()
        try:

            def _monitor_actor() -> None:
                def _on_reconciled(prev: dict, reconciled: Any) -> None:
                    if reconciled is not None:
                        closures.append(dict(reconciled))

                exit_gate.reconcile_exit(
                    pipeline, SYMBOL, 105.0, {},
                    cancel_protection=None,
                    on_reconciled=_on_reconciled,
                )

            def _pipeline_actor() -> None:
                exit_gate.reconcile_exit(
                    pipeline, SYMBOL, 105.0, {},
                    cancel_protection=lambda symbol: None,
                )

            _run_two_actors(_monitor_actor, _pipeline_actor, barrier)
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 1, f"expected exactly one sell, got {len(sells)}"

        final = exit_gate.load_position(SYMBOL)
        assert final is not None
        assert final["tp1_hit"] is True
        assert final["remaining_qty"] == pytest.approx(0.07)


class TestGatePreventsOversellWhereStaleReadsDidNot:
    def test_stale_snapshots_without_gate_duplicate_the_sell(self) -> None:
        """Control: the OLD pattern (each caller reads a stale snapshot,
        then reconciles directly) produces two sells for one position.
        Proves the regression tests above would fail without the gate."""
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)
        snapshot = _base_position()
        barrier = Barrier(2)
        LiveExecutor.enable()
        try:
            _run_two_actors(
                lambda: pipeline.reconcile_position(SYMBOL, 105.0, dict(snapshot)),
                lambda: pipeline.reconcile_position(SYMBOL, 105.0, dict(snapshot)),
                barrier,
            )
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 2, f"expected the duplicated sell without the gate, got {len(sells)}"


class TestPerSymbolLockScope:
    def test_different_symbols_are_not_serialized_against_each_other(self) -> None:
        """The gate is per-symbol, not global: two different symbols can
        be reconciled concurrently without blocking each other."""
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)
        _seed_position(_base_position(symbol="ETH/USDT", tp1=200.0))
        _seed_position(_base_position())
        results: list[str] = []
        barrier = Barrier(2)
        LiveExecutor.enable()
        try:

            def _eth() -> None:
                barrier.wait(timeout=15)
                exit_gate.reconcile_exit(pipeline, "ETH/USDT", 210.0, {}, cancel_protection=None)
                results.append("eth")

            def _btc() -> None:
                barrier.wait(timeout=15)
                exit_gate.reconcile_exit(pipeline, SYMBOL, 105.0, {}, cancel_protection=None)
                results.append("btc")

            _run_two_actors(_eth, _btc, barrier)
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 2
        assert len(results) == 2
