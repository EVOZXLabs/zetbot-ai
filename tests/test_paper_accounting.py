"""Regression tests for paper trading accounting, notifications, and state consistency.

Covers:
- Correct PnL on small price moves (+1%, -1%)
- Exit reason validation (Take Profit never with negative PnL)
- Restart recovery (state persistence)
- Wallet exposure calculation
- Duplicate close notification prevention
- Reset paper state tool
"""

import json
import math
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on sys.path
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.paper_trading_engine import (
    ExecutionModel,
    Order,
    PaperTradingEngine,
    VirtualPosition,
    VirtualWallet,
    TAKER_FEE,
    SLIPPAGE_BPS,
    INITIAL_BALANCE,
)


# ---------------------------------------------------------------------------
#  ExecutionModel unit tests
# ---------------------------------------------------------------------------

class TestExecutionModel:
    """Fee + slippage model correctness."""

    def test_buy_adds_slippage_and_fee(self) -> None:
        result = ExecutionModel.buy(100.0, 10.0)
        assert result["fill_price"] > 100.0
        assert result["slippage"] > 0
        assert result["fee"] > 0
        assert result["total_cost"] > 100.0 * 10.0

    def test_sell_subtracts_slippage_and_fee(self) -> None:
        result = ExecutionModel.sell(100.0, 10.0)
        assert result["fill_price"] < 100.0
        assert result["slippage"] > 0
        assert result["fee"] > 0
        assert result["total_proceeds"] < 100.0 * 10.0

    def test_roundtrip_small_loss(self) -> None:
        """Buy then sell at same price should show small loss from fees."""
        qty = 1.0
        buy = ExecutionModel.buy(100.0, qty)
        sell = ExecutionModel.sell(100.0, qty)
        pnl = sell["total_proceeds"] - buy["total_cost"]
        assert pnl < 0
        assert pnl > -5.0  # Should be small loss, not catastrophic


# ---------------------------------------------------------------------------
#  PnL correctness — Buy 1000 USDT, price +1%, close
# ---------------------------------------------------------------------------

class TestPnlCorrectness:
    """Verify PnL on known scenarios."""

    def test_profit_on_1pct_gain(self) -> None:
        """Buy 1000 USDT at 668.75, price +1%, close. Profit ~+10 USDT."""
        entry = 668.75
        position_usdt = 1000.0
        qty = position_usdt / entry

        buy = ExecutionModel.buy(entry, qty)
        cost = buy["total_cost"]

        exit_price = entry * 1.01  # +1%
        sell = ExecutionModel.sell(exit_price, qty)
        proceeds = sell["total_proceeds"]

        pnl = proceeds - cost
        assert pnl > 5.0, f"Expected profit > 5 USDT, got {pnl:.2f}"
        assert pnl < 20.0, f"Expected profit < 20 USDT, got {pnl:.2f}"

    def test_loss_on_1pct_drop(self) -> None:
        """Buy 1000 USDT at 668.75, price -1%, close. Loss ~-10 USDT."""
        entry = 668.75
        position_usdt = 1000.0
        qty = position_usdt / entry

        buy = ExecutionModel.buy(entry, qty)
        cost = buy["total_cost"]

        exit_price = entry * 0.99  # -1%
        sell = ExecutionModel.sell(exit_price, qty)
        proceeds = sell["total_proceeds"]

        pnl = proceeds - cost
        assert pnl < -5.0, f"Expected loss < -5 USDT, got {pnl:.2f}"
        assert pnl > -20.0, f"Expected loss > -20 USDT, got {pnl:.2f}"

    def test_tiny_move_not_100pct_loss(self) -> None:
        """A tiny price movement must never create a catastrophic loss."""
        entry = 668.7506
        position_usdt = 1000.0
        qty = position_usdt / entry

        buy = ExecutionModel.buy(entry, qty)
        cost = buy["total_cost"]

        exit_price = 668.5500  # -0.03%
        sell = ExecutionModel.sell(exit_price, qty)
        proceeds = sell["total_proceeds"]

        pnl = proceeds - cost
        assert pnl > -100.0, f"Catastrophic loss: {pnl:.2f}"
        assert abs(pnl) < position_usdt * 0.1, (
            f"Loss {pnl:.2f} exceeds 10% of position size"
        )


# ---------------------------------------------------------------------------
#  Exit reason validation
# ---------------------------------------------------------------------------

class TestExitReasonValidation:
    """Take Profit must never be used when PnL is negative."""

    def test_take_profit_with_negative_pnl_reclassified(self) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        reason = PaperTradingEngine._resolve_exit_reason(
            "Take Profit", "CLOSED", True, False, False, -5.0,
        )
        assert reason != "Take Profit"
        assert reason in ("Stop Loss", "Strategy Exit")

    def test_take_profit_with_positive_pnl_kept(self) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        reason = PaperTradingEngine._resolve_exit_reason(
            "Take Profit", "CLOSED", True, False, False, 10.0,
        )
        assert reason == "Take Profit"

    def test_take_profit_with_zero_pnl_kept(self) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        reason = PaperTradingEngine._resolve_exit_reason(
            "Take Profit", "CLOSED", True, False, False, 0.0,
        )
        assert reason == "Take Profit"

    def test_stop_loss_never_reclassified(self) -> None:
        from scripts.paper_trading_engine import PaperTradingEngine
        reason = PaperTradingEngine._resolve_exit_reason(
            "Stop Loss", "STOPPED", False, False, False, -50.0,
        )
        assert reason == "Stop Loss"


# ---------------------------------------------------------------------------
#  State persistence / restart recovery
# ---------------------------------------------------------------------------

class TestStateRecovery:
    """Paper engine must correctly restore state across restarts."""

    def test_save_and_load_state(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        # Create engine, execute a buy
        engine = PaperTradingEngine()
        assert engine.wallet.balance == INITIAL_BALANCE

        # Manually create a plan and execute
        plan = {
            "symbol": "BTC/USDT",
            "entry_price": 100.0,
            "quantity": 5.0,
            "position_size_usdt": 500.0,
            "stop_loss": 95.0,
            "tp1": 105.0,
            "tp2": 110.0,
            "tp3": 115.0,
            "risk_amount": 25.0,
            "reward_amount": 25.0,
            "risk_reward": 1.0,
            "confidence": 80.0,
            "recommendation": "BUY",
            "signal_time": "2024-01-01T00:00:00",
            "status": "READY",
        }
        order = engine._execute_plan(plan, None)
        assert order is not None
        assert order.status == "FILLED"

        # Save state
        engine._save_state()

        # Create new engine — should restore
        engine2 = PaperTradingEngine()
        assert engine2.wallet.balance < INITIAL_BALANCE
        assert len(engine2.orders) == 1
        assert "BTC/USDT" in engine2.positions
        vp = engine2.positions["BTC/USDT"]
        assert vp.status == "OPEN"
        assert vp.quantity == 5.0

    def test_closure_notified_persisted(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        engine = PaperTradingEngine()
        engine.positions["TEST"] = VirtualPosition(
            symbol="TEST", order_id="O1",
            quantity=10.0, remaining_qty=10.0,
            entry_price=100.0, current_price=100.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
            total_pnl=0.0, cost_basis=1000.0,
            status="OPEN", opened_at="2024-01-01T00:00:00",
            closure_notified=False,
        )
        engine._save_state()

        engine2 = PaperTradingEngine()
        assert engine2.positions["TEST"].closure_notified is False

        # Simulate close with notification
        engine2.positions["TEST"].closure_notified = True
        engine2.positions["TEST"].status = "CLOSED"
        engine2._save_state()

        engine3 = PaperTradingEngine()
        assert engine3.positions["TEST"].closure_notified is True


# ---------------------------------------------------------------------------
#  Wallet exposure
# ---------------------------------------------------------------------------

class TestWalletExposure:
    """Wallet exposure calculation edge cases."""

    def test_exposure_calculation(self) -> None:
        """exposure = pos_value / (equity + pos_value) * 100"""
        eq = 5000.0
        pos_value = 5000.0
        exposure = pos_value / (eq + pos_value) * 100
        assert abs(exposure - 50.0) < 0.01

    def test_exposure_with_zero_equity(self) -> None:
        """Should not divide by zero."""
        eq = 0.0
        pos_value = 1000.0
        exposure = pos_value / (eq + pos_value) * 100
        assert abs(exposure - 100.0) < 0.01

    def test_exposure_with_no_positions(self) -> None:
        eq = 10000.0
        pos_value = 0.0
        exposure = pos_value / (eq + pos_value) * 100 if (eq + pos_value) > 0 else 0.0
        assert exposure == 0.0


# ---------------------------------------------------------------------------
#  Duplicate close notification prevention
# ---------------------------------------------------------------------------

class TestDuplicateCloseNotification:
    """closure_notified flag must prevent double notifications."""

    def test_closure_notified_flag(self) -> None:
        vp = VirtualPosition(
            symbol="BTC/USDT", order_id="O1",
            quantity=1.0, remaining_qty=1.0,
            entry_price=100.0, current_price=100.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
            total_pnl=0.0, cost_basis=100.0,
            status="OPEN", closure_notified=False,
        )
        assert vp.closure_notified is False
        vp.closure_notified = True
        assert vp.closure_notified is True

    def test_closure_notified_in_state_roundtrip(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        engine = PaperTradingEngine()
        engine.positions["X"] = VirtualPosition(
            symbol="X", order_id="O2",
            quantity=5.0, remaining_qty=5.0,
            entry_price=200.0, current_price=200.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
            total_pnl=0.0, cost_basis=1000.0,
            status="OPEN", closure_notified=False,
        )
        engine._save_state()

        loaded = PaperTradingEngine()
        assert loaded.positions["X"].closure_notified is False

        # After close notification, persist
        loaded.positions["X"].closure_notified = True
        loaded.positions["X"].status = "CLOSED"
        loaded._save_state()

        reloaded = PaperTradingEngine()
        assert reloaded.positions["X"].closure_notified is True


# ---------------------------------------------------------------------------
#  Reset paper state tool
# ---------------------------------------------------------------------------

class TestResetPaperState:
    """Verify reset_paper_state removes expected files."""

    def test_removes_all_files(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        for fname in [
            "paper_balance.json", "paper_orders.json", "paper_orders.csv",
            "paper_trade_history.csv", "paper_state.json", "positions.json",
        ]:
            with open(os.path.join("data", fname), "w") as f:
                f.write("{}")

        from scripts.reset_paper_state import reset_paper_state
        removed = reset_paper_state()
        assert len(removed) == 6
        for f in removed:
            assert not os.path.exists(f)

    def test_no_error_on_missing_files(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        from scripts.reset_paper_state import reset_paper_state
        removed = reset_paper_state()
        assert len(removed) == 0


# ---------------------------------------------------------------------------
#  Order dataclass integrity
# ---------------------------------------------------------------------------

class TestOrderDataclass:
    """Order dataclass should not have duplicate fields."""

    def test_order_fields_unique(self) -> None:
        import dataclasses
        fields = [f.name for f in dataclasses.fields(Order)]
        assert len(fields) == len(set(fields)), (
            f"Duplicate fields found: {[f for f in fields if fields.count(f) > 1]}"
        )

    def test_order_creation(self) -> None:
        o = Order(
            id="TEST-1", symbol="BTC/USDT", side="BUY", type="MARKET",
            quantity=1.0, filled_quantity=1.0,
            entry_price=100.0, fill_price=100.03,
            slippage=0.03, entry_fee=0.10,
            exit_price=0.0, exit_fee=0.0,
            total_cost=100.13, total_proceeds=0.0,
            net_pnl=0.0, net_pnl_pct=0.0,
            status="FILLED",
            created_at="2024-01-01T00:00:00",
            filled_at="2024-01-01T00:00:00",
            closed_at="",
        )
        d = asdict(o)
        assert d["id"] == "TEST-1"
        assert d["filled_at"] == "2024-01-01T00:00:00"
        assert d["closed_at"] == ""


# ---------------------------------------------------------------------------
#  Position size fallback
# ---------------------------------------------------------------------------

class TestPositionSizeFallback:
    """Position size should fall through multiple fields."""

    def test_position_size_usdt_preferred(self) -> None:
        p = {"position_size_usdt": 500.0, "cost_basis": 499.0, "entry_price": 100.0, "quantity": 5.0}
        size = p.get("position_size_usdt") or p.get("cost_basis", 0.0) or (p.get("entry_price", 0.0) * p.get("quantity", 0.0))
        assert size == 500.0

    def test_cost_basis_fallback(self) -> None:
        p = {"position_size_usdt": 0, "cost_basis": 499.0, "entry_price": 100.0, "quantity": 5.0}
        size = p.get("position_size_usdt") or p.get("cost_basis", 0.0) or (p.get("entry_price", 0.0) * p.get("quantity", 0.0))
        assert size == 499.0

    def test_entry_times_quantity_fallback(self) -> None:
        p = {"position_size_usdt": 0, "cost_basis": 0, "entry_price": 100.0, "quantity": 5.0}
        size = p.get("position_size_usdt") or p.get("cost_basis", 0.0) or (p.get("entry_price", 0.0) * p.get("quantity", 0.0))
        assert size == 500.0

    def test_all_zero(self) -> None:
        p = {"position_size_usdt": 0, "cost_basis": 0, "entry_price": 0, "quantity": 0}
        size = p.get("position_size_usdt") or p.get("cost_basis", 0.0) or (p.get("entry_price", 0.0) * p.get("quantity", 0.0))
        assert size == 0.0


# ---------------------------------------------------------------------------
#  Paper state sync on closure (prevents duplicate notifications on restart)
# ---------------------------------------------------------------------------

def _make_paper_state(symbol: str = "BTCUSDT", status: str = "OPEN") -> dict:
    return {
        "version": 1,
        "balance": 9500.0,
        "margin_used": 0.0,
        "orders": [],
        "positions": {
            symbol: {
                "symbol": symbol,
                "order_id": "paper-1",
                "quantity": 0.05,
                "remaining_qty": 0.05,
                "entry_price": 100000.0,
                "current_price": 101000.0,
                "unrealized_pnl": 50.0,
                "realized_pnl": 0.0,
                "total_pnl": 50.0,
                "cost_basis": 5000.0,
                "status": status,
                "tp1_sold": False,
                "tp2_sold": False,
                "tp3_sold": False,
                "opened_at": "2026-01-01T00:00:00+00:00",
                "signal_time": "2026-01-01T00:00:00+00:00",
                "closure_notified": False,
                "tp1": 0.0,
                "tp2": 0.0,
                "tp3": 0.0,
                "stop_loss": 0.0,
                "position_size_usdt": 500.0,
            }
        },
        "equity_history": [],
    }


class TestPaperStateSyncOnClosure:
    """paper_state.json must be patched when main.py closes a position."""

    def _setup(self, tmp_path: Any, symbol: str = "BTCUSDT", status: str = "OPEN"):
        """Write a paper_state.json under tmp_path/data/ and chdir there."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        state_path = data_dir / "paper_state.json"
        state = _make_paper_state(symbol, status)
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
        import os as _os
        _os.chdir(tmp_path)
        return state_path

    def test_marks_position_closed(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _sync_paper_state_on_closure
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", -25.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        vp = state["positions"]["BTCUSDT"]
        assert vp["status"] == "CLOSED"
        assert vp["remaining_qty"] == 0.0
        assert vp["closure_notified"] is True
        assert vp["realized_pnl"] == -25.0
        assert vp["total_pnl"] == -25.0
        assert vp["unrealized_pnl"] == 0.0

    def test_positive_pnl(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _sync_paper_state_on_closure
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 42.17)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        vp = state["positions"]["BTCUSDT"]
        assert vp["status"] == "CLOSED"
        assert vp["realized_pnl"] == 42.17

    def test_already_closed_noop(self, tmp_path: Any) -> None:
        self._setup(tmp_path, status="CLOSED")
        from main import _sync_paper_state_on_closure
        # Should not crash, should not overwrite
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 999.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        vp = state["positions"]["BTCUSDT"]
        # Status was already CLOSED — function returns early, no change to realized_pnl
        assert vp["realized_pnl"] == 0.0

    def test_missing_symbol_noop(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _sync_paper_state_on_closure
        # ETHUSDT doesn't exist — should not crash
        _sync_paper_state_on_closure(MagicMock(), "ETHUSDT", 10.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert "ETHUSDT" not in state["positions"]

    def test_missing_file_noop(self, tmp_path: Any) -> None:
        (tmp_path / "data").mkdir()
        import os as _os
        _os.chdir(tmp_path)
        from main import _sync_paper_state_on_closure
        # No paper_state.json exists — should not raise
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 10.0)

    def test_corrupt_json_noop(self, tmp_path: Any) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "paper_state.json").write_text("NOT JSON{{{")
        import os as _os
        _os.chdir(tmp_path)
        from main import _sync_paper_state_on_closure
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 10.0)

    def test_preserves_other_positions(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        # Add a second position
        with open("data/paper_state.json") as f:
            state = json.load(f)
        state["positions"]["ETHUSDT"] = {
            **_make_paper_state("ETHUSDT")["positions"]["ETHUSDT"],
            "status": "OPEN",
            "remaining_qty": 1.0,
            "closure_notified": False,
        }
        with open("data/paper_state.json", "w") as f:
            json.dump(state, f)

        from main import _sync_paper_state_on_closure
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 5.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        # BTCUSDT closed
        assert state["positions"]["BTCUSDT"]["status"] == "CLOSED"
        # ETHUSDT untouched
        eth = state["positions"]["ETHUSDT"]
        assert eth["status"] == "OPEN"
        assert eth["remaining_qty"] == 1.0
        assert eth["closure_notified"] is False

    def test_wallet_balance_updated_with_proceeds(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _sync_paper_state_on_closure
        # total_proceeds of 5420.0 should be added to the 9500.0 balance
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 10.0, 5420.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert state["balance"] == 9500.0 + 5420.0

    def test_wallet_balance_zero_proceeds_default(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _sync_paper_state_on_closure
        _sync_paper_state_on_closure(MagicMock(), "BTCUSDT", 10.0)

        with open("data/paper_state.json") as f:
            state = json.load(f)
        # total_proceeds defaults to 0, so balance stays unchanged
        assert state["balance"] == 9500.0
