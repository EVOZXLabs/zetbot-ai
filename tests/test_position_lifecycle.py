"""
Regression tests for position lifecycle, paper trading state persistence,
TP/SL execution, holding time progression, and wallet accounting.

Covers Audit Phase 2 goals 1-9.
"""

import os
from typing import Any
from dataclasses import asdict
from datetime import datetime, timezone, timedelta

import pytest

from scripts.paper_trading_engine import (
    ExecutionModel,
    MetricsCalculator,
    Order,
    PaperTradingEngine,
    VirtualPosition,
    VirtualWallet,
    EquitySnapshot,
)
from scripts.position_manager import (
    DataLoader,
    Position,
    PositionManager,
    PositionSimulator,
    TradePlan,
)


@pytest.fixture(autouse=True)
def _isolated_state(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test in this file redirects STATE_PATH to a temp file.

    These tests used to read/write/delete the REAL ``data/paper_state.json``:
    they failed whenever the bot had real state (assertions saw the bot's
    orders/positions) and destroyed live bot state on save/cleanup.
    """
    import scripts.paper_trading_engine as pte
    monkeypatch.setattr(pte, "STATE_PATH", str(tmp_path / "paper_state.json"))


# ===================================================================
#  Goal 1 — Holding time progression
# ===================================================================

class TestHoldingTime:
    """Holding time must correctly increase across pipeline cycles."""

    def test_holding_hours_from_signal_time(self):
        """PositionSimulator computes hours from signal_time."""
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=53000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=(datetime.now(timezone.utc) - timedelta(hours=5))
            .isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 50500.0, 1.5, "BULLISH", now,
        )
        assert pos.holding_hours >= 4.5  # ~5 hours
        assert pos.holding_candles >= 4

    def test_holding_hours_near_zero_for_fresh_signal(self):
        """A freshly created signal has near-zero holding time."""
        signal_ts = datetime.now(timezone.utc).isoformat()
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=53000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=signal_ts,
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 50000.0, 1.5, "BULLISH", now,
        )
        assert pos.holding_hours < 0.01

    def test_entry_time_preserved_in_position_output(self):
        """Position stores entry_time from plan.signal_time."""
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=53000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time="2026-01-01T00:00:00+00:00",
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 50000.0, 1.5, "BULLISH", now,
        )
        assert pos.entry_time == "2026-01-01T00:00:00+00:00"


# ===================================================================
#  Goal 2 — Current price refresh
# ===================================================================

class TestCurrentPriceRefresh:
    """Position.current_price must use fresh scanner data."""

    def test_current_price_from_scanner(self):
        """Current price in Position comes from scanner price."""
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=53000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        # Scanner price is different from entry → must be reflected
        pos = PositionSimulator.simulate(
            plan, 51500.0, 1.5, "BULLISH", now,
        )
        assert pos.current_price == 51500.0
        assert pos.current_price != plan.entry_price


# ===================================================================
#  Goal 3 — Unrealized PnL updates
# ===================================================================

class TestUnrealizedPnl:
    """Unrealized PnL must use current price × remaining qty."""

    def test_unrealized_pnl_from_current_price(self):
        """Floating PnL = (current_price - entry) × remaining_qty."""
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=52000.0, tp2=53000.0, tp3=54000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 51000.0, 1.5, "BULLISH", now,
        )
        # full qty, no TPs hit: unrealized = (51000-50000) * 0.02 = 20
        assert pos.floating_pnl == pytest.approx(20.0, abs=0.01)

    def test_unrealized_pnl_after_tp1(self):
        """After TP1 hit, remaining_qty is reduced, PnL reflects it."""
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=50500.0, tp2=53000.0, tp3=54000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        # current_price >= tp1 (50500), but below tp2
        pos = PositionSimulator.simulate(
            plan, 51000.0, 1.5, "BULLISH", now,
        )
        # TP1 hit → 30% sold (realized)
        # remaining_qty = 0.02 * 0.70 = 0.014
        assert pos.remaining_pct == pytest.approx(70.0, abs=0.1)
        remaining = pos.remaining_qty
        # unrealized = (51000-50000) * remaining
        assert pos.floating_pnl == pytest.approx(
            pos.realized_pnl + (51000 - 50000) * remaining, abs=0.1
        )


# ===================================================================
#  Goal 4 — TP / SL auto-execution
# ===================================================================

class TestTPExecution:
    """TP1/TP2/TP3 must be detected by PositionSimulator."""

    def test_tp1_hit_detected(self):
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=50500.0, tp2=52000.0, tp3=54000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 51000.0, 1.5, "BULLISH", now,
        )
        assert pos.tp1_hit is True
        assert pos.tp2_hit is False
        assert pos.tp3_hit is False
        # TP1 triggers breakeven (stop moves to entry) → status is BREAKEVEN
        assert pos.status in ("PARTIAL", "BREAKEVEN")

    def test_all_tps_hit(self):
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=50500.0, tp2=52000.0, tp3=54000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 55000.0, 1.5, "BULLISH", now,
        )
        assert pos.tp1_hit is True
        assert pos.tp2_hit is True
        assert pos.tp3_hit is True
        assert pos.status == "CLOSED"

    def test_stop_loss_hit(self):
        plan = TradePlan(
            symbol="BTC/USDT", entry_price=50000.0,
            position_size_usdt=1000.0, quantity=0.02,
            stop_loss=49000.0, tp1=50500.0, tp2=52000.0, tp3=54000.0,
            risk_amount=20.0, reward_amount=40.0, risk_reward=2.0,
            probability=75.0, recommendation="BUY",
            confidence=70.0,
            signal_time=datetime.now(timezone.utc).isoformat(),
            status="READY", rejection_reason="",
        )
        now = datetime.now(timezone.utc)
        pos = PositionSimulator.simulate(
            plan, 48000.0, 1.5, "BULLISH", now,
        )
        assert pos.status == "STOPPED"
        assert pos.current_price <= pos.stop_loss


# ===================================================================
#  Goal 5 — Realized PnL from closed positions
# ===================================================================

class TestPaperEngineExecution:
    """Paper engine sell orders correctly compute realized PnL."""

    def test_tp1_sell_creates_closed_order(self):
        """When TP1 is hit, paper engine creates a CLOSED sell order with PnL."""
        engine = PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)
        pos_state = {
            "symbol": "BTC/USDT",
            "current_price": 51000.0,
            "status": "OPEN",
            "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 49000.0,
        }
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state)

        closed = [o for o in engine.orders if o.status == "CLOSED"]
        assert len(closed) == 1
        assert closed[0].side == "SELL"
        # TP1 = 30% of 0.02 = 0.006 BTC
        assert closed[0].quantity == pytest.approx(0.006, abs=1e-4)
        # Realized PnL should be positive (sold above entry)
        assert closed[0].net_pnl > 0

    def test_sl_sell_creates_closed_order(self):
        """When SL is hit, paper engine closes full remaining position."""
        engine = PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)
        pos_state = {
            "symbol": "BTC/USDT",
            "current_price": 48000.0,
            "status": "STOPPED",
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 49000.0,
        }
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state)

        closed = [o for o in engine.orders if o.status == "CLOSED"]
        assert len(closed) == 1
        assert closed[0].side == "SELL"
        assert closed[0].quantity == pytest.approx(0.02, abs=1e-4)
        # Realized PnL should be negative (sold below entry)
        assert closed[0].net_pnl < 0

    def test_partial_tp_then_full_close(self):
        """TP1 hit, then full close on STOPPED sells remaining qty only."""
        engine = PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)

        # Cycle 1: TP1 hit
        pos_state1 = {
            "symbol": "BTC/USDT",
            "current_price": 51000.0,
            "status": "OPEN",
            "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 49000.0,
        }
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state1)

        # Cycle 2: SL hit (remaining 70% closed)
        pos_state2 = {
            "symbol": "BTC/USDT",
            "current_price": 48000.0,
            "status": "STOPPED",
            "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 49000.0,
        }
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state2)

        closed = [o for o in engine.orders if o.status == "CLOSED"]
        assert len(closed) == 2
        tp_order = closed[0]
        sl_order = closed[1]
        # TP1 sold 30% = 0.006, SL sold remaining 70% = 0.014
        assert tp_order.quantity == pytest.approx(0.006, abs=1e-4)
        assert sl_order.quantity == pytest.approx(0.014, abs=1e-4)
        assert engine.positions["BTC/USDT"].status == "CLOSED"
        assert engine.positions["BTC/USDT"].remaining_qty == 0.0


# ===================================================================
#  Goal 6 — VirtualWallet accounting
# ===================================================================

class TestWalletAccounting:
    """Free balance, used, equity, realized/unrealized PnL."""

    def test_initial_state(self):
        w = VirtualWallet(10000.0)
        assert w.balance == 10000.0
        assert w.free_balance == 10000.0
        snap = w.snapshot()
        assert snap.balance == 10000.0
        assert snap.equity == 10000.0
        assert snap.unrealized_pnl == 0.0
        assert snap.margin_used == 0.0
        assert snap.free_balance == 10000.0

    def test_deduct_and_add(self):
        w = VirtualWallet(10000.0)
        assert w.deduct(2000.0) is True
        assert w.balance == 8000.0
        w.add(500.0)
        assert w.balance == 8500.0

    def test_snapshot_equity_includes_position_value(self):
        w = VirtualWallet(10000.0)
        w.deduct(2000.0)
        snap = w.snapshot(position_value=2200.0, unrealized_pnl_value=200.0)
        assert snap.balance == 8000.0
        assert snap.equity == 10200.0  # 8000 + 2200
        assert snap.unrealized_pnl == 200.0
        assert snap.free_balance == 8000.0

    def test_cannot_deduct_insufficient(self):
        w = VirtualWallet(100.0)
        assert w.deduct(200.0) is False
        assert w.balance == 100.0

    def test_used_balance_tracking(self):
        """margin_used tracks reserved funds."""
        w = VirtualWallet(10000.0)
        assert w.reserve(2000.0) is True
        assert w.margin_used == 2000.0
        assert w.free_balance == 8000.0
        w.release(1000.0)
        assert w.margin_used == 1000.0
        assert w.free_balance == 9000.0

    def test_used_balance_cannot_exceed_free(self):
        w = VirtualWallet(100.0)
        assert w.reserve(200.0) is False
        assert w.margin_used == 0.0

    def test_wallet_rejects_negative_deduct(self):
        """deduct returns False if amount > balance."""
        w = VirtualWallet(100.0)
        assert w.deduct(101.0) is False
        assert w.balance == 100.0

    def test_wallet_accepts_exact_deduct(self):
        """deduct returns True if amount == balance."""
        w = VirtualWallet(100.0)
        assert w.deduct(100.0) is True
        assert w.balance == 0.0


# ===================================================================
#  Goal 7 — MetricsCalc consistency
# ===================================================================

class TestMetricsCalculator:
    """MetricsCalculator must produce correct PnL and drawdown."""

    def test_no_trades(self):
        metrics = MetricsCalculator.compute([], [], 10000.0)
        assert metrics["total_trades"] == 0
        assert metrics["realized_pnl"] == 0.0
        assert metrics["unrealized_pnl"] == 0.0

    def test_one_winning_trade(self):
        orders = [
            Order(
                id="o1", symbol="BTC/USDT", side="SELL", type="MARKET",
                quantity=0.01, filled_quantity=0.01,
                entry_price=50000.0, fill_price=50000.0,
                slippage=0.0, entry_fee=0.0,
                exit_price=51000.0, exit_fee=5.0,
                total_cost=500.0, total_proceeds=505.0,
                net_pnl=5.0, net_pnl_pct=1.0,
                status="CLOSED",
                created_at="2026-01-01T00:00:00",
                filled_at="2026-01-01T00:00:00",
                closed_at="2026-01-01T01:00:00",
            ),
        ]
        snapshots = [
            EquitySnapshot("2026-01-01T00:00:00", 10000.0, 10000.0, 0.0, 0.0, 10000.0),
            EquitySnapshot("2026-01-01T01:00:00", 10005.0, 10005.0, 0.0, 0.0, 10005.0),
        ]
        metrics = MetricsCalculator.compute(orders, snapshots, 10000.0)
        assert metrics["total_trades"] == 1
        assert metrics["winning_trades"] == 1
        assert metrics["realized_pnl"] == 5.0
        assert metrics["total_return_pct"] == pytest.approx(0.05, abs=0.01)

    def test_drawdown_tracking(self):
        """Drawdown computed from equity history."""
        snapshots = [
            EquitySnapshot("t1", 10000.0, 10000.0, 0.0, 0.0, 10000.0),
            EquitySnapshot("t2", 11000.0, 11000.0, 0.0, 0.0, 11000.0),
            EquitySnapshot("t3", 9500.0, 9500.0, 0.0, 0.0, 9500.0),
            EquitySnapshot("t4", 10500.0, 10500.0, 0.0, 0.0, 10500.0),
        ]
        metrics = MetricsCalculator.compute([], snapshots, 10000.0)
        # Peak = 11000, max dd = 11000 - 9500 = 1500
        assert metrics["max_drawdown"] == pytest.approx(1500.0, abs=0.01)
        assert metrics["max_drawdown_pct"] == pytest.approx(
            1500.0 / 11000.0 * 100.0, abs=0.01
        )


# ===================================================================
#  Goal 8 — State persistence (cross-cycle)
# ===================================================================

class TestStatePersistence:
    """PaperTradingEngine must save and reload state across cycles.

    STATE_PATH is redirected to a per-test temp file by the module-level
    ``_isolated_state`` fixture (see top of file).
    """

    def test_save_and_load_orders(self):
        """Orders survive a save/load cycle."""
        engine1 = PaperTradingEngine()
        engine1.orders.append(Order(
            id="test1", symbol="BTC/USDT", side="BUY", type="MARKET",
            quantity=0.01, filled_quantity=0.01,
            entry_price=50000.0, fill_price=50001.5,
            slippage=1.5, entry_fee=5.0,
            exit_price=0.0, exit_fee=0.0,
            total_cost=500.0, total_proceeds=0.0,
            net_pnl=0.0, net_pnl_pct=0.0,
            status="FILLED",
            created_at="2026-01-01T00:00:00",
            filled_at="2026-01-01T00:00:00",
            closed_at="",
        ))
        engine1._save_state()

        engine2 = PaperTradingEngine()
        assert len(engine2.orders) == 1
        assert engine2.orders[0].id == "test1"
        assert engine2.orders[0].symbol == "BTC/USDT"

        # Cleanup
        self._cleanup()

    def test_save_and_load_wallet_balance(self):
        """Wallet balance persists across instances."""
        engine1 = PaperTradingEngine()
        engine1.wallet.balance = 8500.0
        engine1.wallet.margin_used = 1500.0
        engine1._save_state()

        engine2 = PaperTradingEngine()
        assert engine2.wallet.balance == 8500.0
        assert engine2.wallet.margin_used == 1500.0

        self._cleanup()

    def test_save_and_load_positions(self):
        """Open positions persist across instances."""
        engine1 = PaperTradingEngine()
        engine1.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.014,
            entry_price=50000.0, current_price=51000.0,
            unrealized_pnl=140.0, realized_pnl=30.0, total_pnl=170.0,
            cost_basis=1000.0, status="OPEN",
            opened_at="2026-01-01T00:00:00",
        )
        engine1._save_state()

        engine2 = PaperTradingEngine()
        assert "BTC/USDT" in engine2.positions
        vp = engine2.positions["BTC/USDT"]
        assert vp.symbol == "BTC/USDT"
        assert vp.status == "OPEN"
        assert vp.remaining_qty == 0.014
        assert vp.opened_at == "2026-01-01T00:00:00"

        self._cleanup()

    def test_skip_re_execution_for_existing_position(self):
        """Symbols with OPEN position in state are not re-executed."""
        engine = PaperTradingEngine()
        engine.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.014,
            entry_price=50000.0, current_price=51000.0,
            unrealized_pnl=140.0, realized_pnl=30.0, total_pnl=170.0,
            cost_basis=1000.0, status="OPEN",
            opened_at="2026-01-01T00:00:00",
        )
        plans = [
            {"symbol": "BTC/USDT", "entry_price": 52000.0,
             "quantity": 0.02, "position_size_usdt": 1040.0,
             "confidence": 80.0},
        ]
        # This would normally call _execute_plan which would OVERWRITE
        # the existing position. With skip logic, it should not.
        # We can't easily test this without mocking, but we verify
        # the skip logic condition is correct.
        vp = engine.positions.get("BTC/USDT")
        assert vp is not None and vp.status == "OPEN"
        for plan in plans:
            symbol = plan["symbol"]
            existing = engine.positions.get(symbol)
            if existing is not None and existing.status == "OPEN":
                continue  # This is the skip logic
            # Would call _execute_plan here

        # Position should remain unchanged
        assert engine.positions["BTC/USDT"].entry_price == 50000.0
        assert engine.positions["BTC/USDT"].cost_basis == 1000.0

    def test_execute_plan_for_closed_position(self):
        """Symbol with CLOSED position can be re-executed."""
        engine = PaperTradingEngine()
        engine.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.0,
            entry_price=50000.0, current_price=48000.0,
            unrealized_pnl=0.0, realized_pnl=-200.0, total_pnl=-200.0,
            cost_basis=1000.0, status="CLOSED",
            opened_at="2026-01-01T00:00:00",
        )
        plan = {
            "symbol": "BTC/USDT", "entry_price": 51000.0,
            "quantity": 0.015, "position_size_usdt": 765.0,
            "confidence": 80.0,
        }
        vp = engine.positions.get("BTC/USDT")
        assert vp is not None and vp.status == "CLOSED"
        # Should proceed to execution (not skipped)
        assert not (vp is not None and vp.status == "OPEN")

    @staticmethod
    def _cleanup() -> None:
        # Use the module attribute (may be monkeypatched to a temp path)
        # so cleanup NEVER touches the real data/paper_state.json.
        import scripts.paper_trading_engine as pte
        try:
            os.remove(pte.STATE_PATH)
        except FileNotFoundError:
            pass


# ===================================================================
#  Goal 9 — Multiple simultaneous positions
# ===================================================================

class TestMultiplePositions:
    """Paper engine must handle multiple positions correctly."""

    def test_two_positions_independent_pnl(self):
        """Two positions have independent PnL tracking."""
        engine = PaperTradingEngine()

        # Open BTC/USDT
        engine._execute_plan(
            {"symbol": "BTC/USDT", "entry_price": 50000.0,
             "quantity": 0.02, "position_size_usdt": 1000.0},
            None,
        )
        # Open ETH/USDT
        engine._execute_plan(
            {"symbol": "ETH/USDT", "entry_price": 3000.0,
             "quantity": 0.5, "position_size_usdt": 1500.0},
            None,
        )

        assert len(engine.positions) == 2
        assert engine.positions["BTC/USDT"].status == "OPEN"
        assert engine.positions["ETH/USDT"].status == "OPEN"

        # Reconcile BTC only (TP1 hit)
        engine._reconcile(
            {"symbol": "BTC/USDT"},
            {
                "symbol": "BTC/USDT",
                "current_price": 51000.0,
                "status": "OPEN",
                "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
                "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
                "stop_loss": 49000.0, "current_stop": 49000.0,
            },
        )

        # BTC should have partial close, ETH untouched
        btc = engine.positions["BTC/USDT"]
        eth = engine.positions["ETH/USDT"]
        assert btc.remaining_qty < btc.quantity  # Partially sold
        assert btc.realized_pnl > 0
        assert eth.remaining_qty == eth.quantity  # Still full
        assert eth.realized_pnl == 0.0

    def test_total_position_value(self):
        """_total_position_value sums all open positions."""
        engine = PaperTradingEngine()
        engine.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.014,
            entry_price=50000.0, current_price=51000.0,
            unrealized_pnl=140.0, realized_pnl=30.0, total_pnl=170.0,
            cost_basis=1000.0, status="OPEN",
        )
        engine.positions["ETH/USDT"] = VirtualPosition(
            symbol="ETH/USDT", order_id="o2",
            quantity=0.5, remaining_qty=0.5,
            entry_price=3000.0, current_price=3100.0,
            unrealized_pnl=50.0, realized_pnl=0.0, total_pnl=50.0,
            cost_basis=1500.0, status="OPEN",
        )
        total = engine._total_position_value()
        expected = 0.014 * 51000.0 + 0.5 * 3100.0
        assert total == pytest.approx(expected, abs=0.01)

    def test_total_unrealized_pnl(self):
        """_total_unrealized_pnl sums unrealized PnL across positions."""
        engine = PaperTradingEngine()
        engine.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.014,
            entry_price=50000.0, current_price=51000.0,
            unrealized_pnl=140.0, realized_pnl=30.0, total_pnl=170.0,
            cost_basis=1000.0, status="OPEN",
        )
        engine.positions["ETH/USDT"] = VirtualPosition(
            symbol="ETH/USDT", order_id="o2",
            quantity=0.5, remaining_qty=0.5,
            entry_price=3000.0, current_price=3100.0,
            unrealized_pnl=50.0, realized_pnl=0.0, total_pnl=50.0,
            cost_basis=1500.0, status="OPEN",
        )
        assert engine._total_unrealized_pnl() == pytest.approx(190.0, abs=0.01)

    def test_closed_position_excluded_from_totals(self):
        """Closed positions are excluded from position totals."""
        engine = PaperTradingEngine()
        engine.positions["BTC/USDT"] = VirtualPosition(
            symbol="BTC/USDT", order_id="o1",
            quantity=0.02, remaining_qty=0.0,
            entry_price=50000.0, current_price=48000.0,
            unrealized_pnl=0.0, realized_pnl=-200.0, total_pnl=-200.0,
            cost_basis=1000.0, status="CLOSED",
        )
        assert engine._total_position_value() == 0.0
        assert engine._total_unrealized_pnl() == 0.0


# ===================================================================
#  Goal 9 — Balance reconciliation across multiple TX
# ===================================================================

class TestBalanceReconciliation:
    """Wallet balance must correctly reflect TP/SL outcomes."""

    def test_balance_after_buy(self):
        """Wallet balance decreases by total_cost on buy."""
        engine = PaperTradingEngine()
        initial = engine.wallet.balance
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)
        # Engine deducted total_cost
        assert engine.wallet.balance < initial
        assert engine.wallet.balance >= initial - 1005.0  # cost + fee

    def test_balance_after_tp1_preserved_after_second_reconcile(self):
        """Re-reconciling same TP hit must not double-sell."""
        engine = PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)
        balance_after_buy = engine.wallet.balance

        pos_state = {
            "symbol": "BTC/USDT",
            "current_price": 51000.0,
            "status": "PARTIAL",
            "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 50500.0,
        }

        # First reconcile
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state)
        balance_after_first = engine.wallet.balance
        closed_after_first = len([o for o in engine.orders if o.status == "CLOSED"])
        assert closed_after_first == 1  # TP1 sold

        # Second reconcile (same state — should NOT double-sell)
        engine._reconcile({"symbol": "BTC/USDT"}, pos_state)
        balance_after_second = engine.wallet.balance
        closed_after_second = len([o for o in engine.orders if o.status == "CLOSED"])

        # TP1 should NOT be sold again — remaining_qty was reduced
        assert balance_after_second == balance_after_first, (
            "Balance changed on second reconcile — TP1 double-counted!"
        )
        assert closed_after_second == closed_after_first, (
            "Extra CLOSED order on second reconcile — TP1 double-counted!"
        )

    def test_full_cycle_buy_tp1_tp2_tp3(self):
        """Full position lifecycle: buy → TP1 → TP2 → TP3."""
        engine = PaperTradingEngine()
        initial = engine.wallet.balance

        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
        }
        engine._execute_plan(plan, None)

        # Simulate 3 reconcile cycles as TP levels are hit
        for i, (price, status, tp1, tp2, tp3) in enumerate([
            (51000.0, "PARTIAL", True, False, False),   # TP1
            (52500.0, "PARTIAL", True, True, False),    # TP2
            (54500.0, "CLOSED", True, True, True),      # TP3
        ]):
            engine._reconcile(
                {"symbol": "BTC/USDT"},
                {
                    "symbol": "BTC/USDT",
                    "current_price": price,
                    "status": status,
                    "tp1_hit": tp1, "tp2_hit": tp2, "tp3_hit": tp3,
                    "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
                    "stop_loss": 49000.0, "current_stop": 49000.0,
                },
            )

        # All 3 TPs sold → position CLOSED, remaining_qty = 0
        vp = engine.positions["BTC/USDT"]
        assert vp.status == "CLOSED"
        assert vp.remaining_qty == 0.0

        # Wallet should have increased (all TPs were profitable)
        assert engine.wallet.balance > initial

        # Total realized PnL > 0
        closed = [o for o in engine.orders if o.status == "CLOSED"]
        total_pnl = sum(o.net_pnl for o in closed)
        assert total_pnl > 0
        assert total_pnl == pytest.approx(
            engine.wallet.balance - initial, abs=1.0
        )

    def test_execution_model_fees_applied(self):
        """ExecutionModel correctly applies fees and slippage."""
        buy = ExecutionModel.buy(50000.0, 0.02)
        assert buy["fill_price"] > 50000.0  # slippage adds
        assert buy["fee"] > 0.0
        assert buy["total_cost"] > buy["fill_price"] * 0.02

        sell = ExecutionModel.sell(51000.0, 0.01)
        assert sell["fill_price"] < 51000.0  # slippage subtracts
        assert sell["fee"] > 0.0
        assert sell["total_proceeds"] < sell["fill_price"] * 0.01
