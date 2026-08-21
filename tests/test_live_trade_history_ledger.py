"""Regression tests for the LIVE trade-history ledger (actual fills).

Root cause (fixed): LIVE trades were never recorded anywhere — MetricsManager
always read ``paper_trade_history.csv`` and ``_account_live`` hardcoded
``realized_pnl=0.0``. Fix: ``scripts/live_trade_ledger`` appends ONE record
per fully-closed LIVE position to ``data/live_trade_history.jsonl`` from the
ACTUAL exchange fills; ``MetricsManager`` becomes mode-aware.

These tests never touch a real exchange or the real ``data/`` dir: every
``data/...`` path is redirected into ``tmp_path`` via ``monkeypatch.chdir``
and CCXT is a recording fake.
"""

import json
import os
import threading
from typing import Any

import pytest

from scripts.execution_engine import LiveExecutor
from scripts.execution_pipeline import ExecutionPipeline
from scripts.execution_provider import (
    LiveExecutionProvider,
    OrderResult,
    PaperExecutionProvider,
)
from scripts.metrics_manager import MetricsManager
import scripts.live_trade_ledger as ltl

SYMBOL = "BTC/USDT"

PAPER_FILES = (
    "data/paper_balance.json",
    "data/paper_orders.json",
    "data/paper_state.json",
    "data/paper_trade_history.csv",
)


class _RecordingExchange:
    last = 110.0  # market price used when a market order omits price

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self._guard = threading.Lock()

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            self.orders.append(kwargs)
            order_id = f"id{len(self.orders)}"
        price = float(kwargs.get("price") or 0) or self.last
        amount = float(kwargs["amount"])
        return {
            "id": order_id,
            "status": "closed",
            "filled": amount,
            "average": price,
            "price": price,
            "cost": amount * price,
            "fee": {"cost": 1.0},
        }


class _FakeProvider:
    def __init__(self, exchange: _RecordingExchange) -> None:
        self._ex = exchange

    def _get_exchange(self) -> _RecordingExchange:
        return self._ex

    def fetch_balance(self) -> dict[str, Any]:
        return {"free": {"BTC": 1.0, "USDT": 100000.0}}

    def get_ticker(self, symbol: str) -> dict[str, float]:
        return {"last": self._ex.last, "ask": self._ex.last, "bid": self._ex.last}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        return price

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        return {}

    def market_buy_requires_price(self) -> bool:
        return False


class _FakeManager:
    name = "binance"

    def __init__(self, provider: _FakeProvider) -> None:
        self._provider = provider

    def get_provider(self) -> _FakeProvider:
        return self._provider


class _FakeConfig:
    quote_currency = "USDT"
    exchange = "binance"


class _Wallet:
    balance = 5000.0


@pytest.fixture
def _live_env(tmp_path, monkeypatch) -> None:
    """Redirect ``data/...`` into ``tmp_path`` and reset the live-arm flag."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


def _make_pipeline(exchange: _RecordingExchange) -> ExecutionPipeline:
    manager = _FakeManager(_FakeProvider(exchange))
    provider = LiveExecutionProvider(manager, _FakeConfig())
    return ExecutionPipeline(provider)


def _order_result(
    symbol: str,
    side: str,
    order_id: str,
    qty: float,
    price: float,
    fee: float,
    ts: str,
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        symbol=symbol,
        side=side,
        status="FILLED",
        filled_amount=qty,
        filled_price=price,
        fee=fee,
        cost=qty * price,
        mode="LIVE",
        timestamp=ts,
    )


def _read_ledger() -> list[dict[str, Any]]:
    try:
        with open("data/live_trade_history.jsonl") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (FileNotFoundError, OSError):
        return []


def _read_pending() -> dict[str, Any]:
    try:
        with open("data/live_pending_closures.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class TestLedgerUnit:
    def test_single_leg_close_writes_exactly_one_record(self, _live_env) -> None:
        buy = _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.1,
                            "2026-01-01T00:00:00+00:00")
        sell = _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                             "2026-01-02T00:00:00+00:00")

        ltl.record_live_entry(buy)
        ltl.record_live_exit_fill(sell, reason="Take Profit")

        trades = _read_ledger()
        assert len(trades) == 1
        rec = trades[0]
        assert rec["symbol"] == SYMBOL
        assert rec["quantity"] == pytest.approx(1.0)
        assert rec["entry_price"] == pytest.approx(10.0)
        assert rec["exit_price"] == pytest.approx(12.0)
        assert rec["entry_fee"] == pytest.approx(0.1)
        assert rec["exit_fee"] == pytest.approx(0.2)
        # gross = 12*1 - 10*1 = 2.0 ; net = 2.0 - 0.1 - 0.2 = 1.7
        assert rec["gross_pnl"] == pytest.approx(2.0)
        assert rec["net_pnl"] == pytest.approx(1.7)
        assert rec["net_pnl_pct"] == pytest.approx(17.0)
        assert rec["buy_order_id"] == "b1"
        assert rec["sell_order_id"] == "s1"
        assert rec["exit_reason"] == "Take Profit"
        assert rec["holding_duration"] == pytest.approx(86400.0)
        assert set(rec) >= {
            "trade_id", "symbol", "quantity", "entry_price", "exit_price",
            "entry_fee", "exit_fee", "gross_pnl", "net_pnl", "net_pnl_pct",
            "opened_at", "closed_at", "holding_duration", "exit_reason",
            "buy_order_id", "sell_order_id",
        }
        # pending must be cleared after finalize
        assert _read_pending() == {}

    def test_no_duplicate_when_reconciliation_reruns(self, _live_env) -> None:
        buy = _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.1,
                            "2026-01-01T00:00:00+00:00")
        sell = _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                             "2026-01-02T00:00:00+00:00")

        ltl.record_live_entry(buy)
        ltl.record_live_exit_fill(sell, reason="Take Profit")
        assert len(_read_ledger()) == 1

        # Simulate a restart + reconciliation re-run replaying the same fills.
        ltl.record_live_entry(buy)
        ltl.record_live_exit_fill(sell, reason="Take Profit")
        assert len(_read_ledger()) == 1

    def test_partial_tp_finalizes_once_after_restart(self, _live_env) -> None:
        buy = _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.1,
                            "2026-01-01T00:00:00+00:00")
        ltl.record_live_entry(buy)

        tp1 = _order_result(SYMBOL, "SELL", "s1", 0.3, 12.0, 0.1,
                            "2026-01-02T00:00:00+00:00")
        ltl.record_live_exit_fill(tp1, reason="Take Profit")
        assert _read_ledger() == [], "partial TP must not finalize yet"
        assert SYMBOL in _read_pending(), "pending must survive (crash/restart)"

        # After a restart the pending record is still on disk.
        tp2 = _order_result(SYMBOL, "SELL", "s2", 0.3, 13.0, 0.1,
                            "2026-01-03T00:00:00+00:00")
        ltl.record_live_exit_fill(tp2, reason="Take Profit")
        assert _read_ledger() == [], "still not fully closed"

        tp3 = _order_result(SYMBOL, "SELL", "s3", 0.4, 14.0, 0.1,
                            "2026-01-04T00:00:00+00:00")
        ltl.record_live_exit_fill(tp3, reason="Take Profit")

        trades = _read_ledger()
        assert len(trades) == 1, "one record for the whole position"
        rec = trades[0]
        # weighted avg exit = (0.3*12 + 0.3*13 + 0.4*14)/1.0 = 13.1
        assert rec["exit_price"] == pytest.approx(13.1)
        assert rec["exit_fee"] == pytest.approx(0.3)
        assert rec["exit_reason"] == "Take Profit"
        assert rec["sell_order_id"] == "s3"
        assert _read_pending() == {}

    def test_mixed_tp_then_sl_reason_is_tpsl(self, _live_env) -> None:
        buy = _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.0,
                            "2026-01-01T00:00:00+00:00")
        ltl.record_live_entry(buy)
        tp = _order_result(SYMBOL, "SELL", "s1", 0.3, 12.0, 0.0,
                           "2026-01-02T00:00:00+00:00")
        ltl.record_live_exit_fill(tp, reason="Take Profit")
        sl = _order_result(SYMBOL, "SELL", "s2", 0.7, 8.0, 0.0,
                           "2026-01-03T00:00:00+00:00")
        ltl.record_live_exit_fill(sl, reason="Stop Loss")

        rec = _read_ledger()[0]
        assert rec["exit_reason"] == "TP/SL"
        assert rec["exit_price"] == pytest.approx((0.3 * 12 + 0.7 * 8))
        assert rec["gross_pnl"] == pytest.approx(0.3 * 12 + 0.7 * 8 - 10.0)

    def test_no_entry_no_estimate(self, _live_env) -> None:
        sell = _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                             "2026-01-02T00:00:00+00:00")
        ltl.record_live_exit_fill(sell, reason="Stop Loss")
        assert _read_ledger() == [], "must refuse to record without entry data"

    def test_fallback_to_positions_json_when_snapshot_missing(self, _live_env) -> None:
        sell = _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                             "2026-01-02T00:00:00+00:00")
        fallback = {
            "symbol": SYMBOL,
            "entry_price": 10.0,
            "quantity": 1.0,
            "remaining_qty": 1.0,
            "opened_at": "2026-01-01T00:00:00+00:00",
        }
        ltl.record_live_exit_fill(sell, reason="Stop Loss",
                                  entry_fallback=fallback)
        rec = _read_ledger()[0]
        assert rec["entry_price"] == pytest.approx(10.0)
        assert rec["buy_order_id"] == "", "no real buy order id to attach"
        assert rec["sell_order_id"] == "s1"

    def test_new_buy_replaces_abandoned_pending(self, _live_env) -> None:
        old_buy = _order_result(SYMBOL, "BUY", "old", 1.0, 10.0, 0.1,
                                "2020-01-01T00:00:00+00:00")
        ltl.record_live_entry(old_buy)
        assert SYMBOL in _read_pending()

        new_buy = _order_result(SYMBOL, "BUY", "new", 2.0, 20.0, 0.2,
                                "2026-01-01T00:00:00+00:00")
        ltl.record_live_entry(new_buy)
        pending = _read_pending()[SYMBOL]
        assert pending["buy_order_id"] == "new"
        assert pending["quantity"] == pytest.approx(2.0)

    def test_live_mode_never_creates_paper_files(self, _live_env) -> None:
        buy = _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.1,
                            "2026-01-01T00:00:00+00:00")
        sell = _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                             "2026-01-02T00:00:00+00:00")
        ltl.record_live_entry(buy)
        ltl.record_live_exit_fill(sell, reason="Take Profit")
        for f in PAPER_FILES:
            assert not os.path.exists(f), f"LIVE ledger must never create {f}"


class TestPipelineIntegration:
    def test_full_sl_close_via_pipeline_records_one_trade(self, _live_env) -> None:
        exchange = _RecordingExchange()
        pipeline = _make_pipeline(exchange)

        plan = {
            "symbol": SYMBOL,
            "entry_price": 110.0,
            "quantity": 0.1,
            "position_size_usdt": 11.0,
            "stop_loss": 110.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "signal_time": "2026-01-01T00:00:00+00:00",
        }
        LiveExecutor.enable()
        try:
            buy_result = pipeline.execute_plan(plan)
            assert buy_result.status == "FILLED"

            position = {
                "symbol": SYMBOL,
                "status": "OPEN",
                "entry_price": 110.0,
                "quantity": 0.1,
                "remaining_qty": 0.1,
                "cost_basis": 11.0,
                "stop_loss": 110.0,
                "tp1": 0.0,
                "tp2": 0.0,
                "tp3": 0.0,
                "tp1_hit": False,
                "realized_pnl": 0.0,
            }
            from scripts.exit_gate import save_position  # noqa: PLC0415
            save_position(SYMBOL, position)

            # Price drops to 100: the market sell fill follows the market.
            exchange.last = 100.0
            reconciled = pipeline.reconcile_position(SYMBOL, 100.0, position, plan={})
            assert reconciled is not None
            assert reconciled["status"] in ("CLOSED", "STOPPED")
        finally:
            LiveExecutor.disable()

        trades = _read_ledger()
        assert len(trades) == 1, "one ledger record per closed LIVE position"
        rec = trades[0]
        assert rec["symbol"] == SYMBOL
        assert rec["quantity"] == pytest.approx(0.1)
        assert rec["entry_price"] == pytest.approx(110.0)  # actual buy fill
        assert rec["exit_price"] == pytest.approx(100.0)   # actual sell fill
        assert rec["exit_reason"] == "Stop Loss"
        assert rec["buy_order_id"] == "id1"
        assert rec["sell_order_id"] == "id2"
        # gross = 0.1*100 - 0.1*110 = -1.0 ; net = -1.0 - 1.0 - 1.0 = -3.0
        assert rec["gross_pnl"] == pytest.approx(-1.0)
        assert rec["net_pnl"] == pytest.approx(-3.0)

        # The LIVE path must never create paper accounting files.
        for f in PAPER_FILES:
            assert not os.path.exists(f), f"LIVE must never create {f}"

    def test_paper_pipeline_writes_no_live_ledger(self, _live_env) -> None:
        pipeline = ExecutionPipeline(PaperExecutionProvider())
        plan = {
            "symbol": SYMBOL,
            "entry_price": 110.0,
            "quantity": 0.1,
            "position_size_usdt": 11.0,
        }
        result = pipeline.execute_plan(plan)
        assert result.status == "FILLED"
        assert not os.path.exists("data/live_trade_history.jsonl")
        assert not os.path.exists("data/live_pending_closures.json")


class TestMetricsLiveMode:
    def test_trade_history_and_account_read_ledger(self, _live_env) -> None:
        ltl.record_live_entry(
            _order_result(SYMBOL, "BUY", "b1", 1.0, 10.0, 0.1,
                          "2026-01-01T00:00:00+00:00"))
        ltl.record_live_exit_fill(
            _order_result(SYMBOL, "SELL", "s1", 1.0, 12.0, 0.2,
                          "2026-01-02T00:00:00+00:00"),
            reason="Take Profit")
        ltl.record_live_entry(
            _order_result("ETH/USDT", "BUY", "b2", 2.0, 100.0, 1.0,
                          "2026-01-03T00:00:00+00:00"))
        ltl.record_live_exit_fill(
            _order_result("ETH/USDT", "SELL", "s2", 2.0, 90.0, 1.0,
                          "2026-01-04T00:00:00+00:00"),
            reason="Stop Loss")

        mgr = MetricsManager(
            "data",
            wallet=_Wallet(),
            mode_provider=lambda: "LIVE",
        )

        trades = mgr.trade_history()
        assert len(trades) == 2
        assert trades[0]["symbol"] == "ETH/USDT"  # newest exit first
        assert trades[0]["net_pnl"] == pytest.approx(-22.0)
        assert trades[1]["net_pnl"] == pytest.approx(1.7)
        assert trades[0]["reason"] == "Stop Loss"

        snap = mgr.account()
        assert snap.balance == pytest.approx(5000.0)
        # realized = 1.7 + (-22.0) = -20.3
        assert snap.realized_pnl == pytest.approx(-20.3)
        assert snap.total_trades == 2
        assert snap.winning_trades == 1
        assert snap.losing_trades == 1
        assert snap.win_rate == pytest.approx(50.0)

    def test_paper_default_still_reads_csv(self, _live_env) -> None:
        mgr = MetricsManager("data")  # no mode_provider -> PAPER behavior
        assert mgr.trade_history() == []
        assert not os.path.exists("data/live_trade_history.jsonl")
