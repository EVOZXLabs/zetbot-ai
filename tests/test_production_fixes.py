"""Regression tests for production fixes (BUG #1 and BUG #2).

BUG #1: BUY execution sends BUY_OPENED notification
BUG #2: Equity calculation includes open position values
"""
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================================
#  BUG #1: BUY_OPENED notification sent on new BUY fill
# ============================================================================

class TestBuyOpenedNotificationOnFill:
    """PaperTradingEngine must send BUY_OPENED notification when a new
    BUY order is filled, and notification failure must never break trading."""

    def test_notify_buy_called_on_execute_plan(self, tmp_path: Any) -> None:
        """_execute_plan calls _notify_buy after creating a filled order."""
        from scripts.paper_trading_engine import PaperTradingEngine

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        mock_notifier = MagicMock()
        engine = PaperTradingEngine(notifier=mock_notifier)

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
            "reasons": ["EMA200 cross"],
        }
        order = engine._execute_plan(plan, None)
        assert order is not None
        assert order.status == "FILLED"

        # Verify _notify_buy was called
        mock_notifier.notify_buy_opened.assert_called_once()
        call_kwargs = mock_notifier.notify_buy_opened.call_args[1]
        assert call_kwargs["symbol"] == "BTC/USDT"
        assert call_kwargs["entry_price"] > 0
        assert call_kwargs["quantity"] > 0

    def test_notification_failure_does_not_break_trading(self, tmp_path: Any) -> None:
        """If the notifier raises, the order and position must still be created."""
        from scripts.paper_trading_engine import PaperTradingEngine

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        failing_notifier = MagicMock()
        failing_notifier.notify_buy_opened.side_effect = RuntimeError("Telegram down")
        engine = PaperTradingEngine(notifier=failing_notifier)

        plan = {
            "symbol": "ETH/USDT",
            "entry_price": 200.0,
            "quantity": 2.0,
            "position_size_usdt": 400.0,
            "stop_loss": 190.0,
            "tp1": 210.0,
            "tp2": 220.0,
            "tp3": 230.0,
            "risk_amount": 20.0,
            "reward_amount": 20.0,
            "risk_reward": 1.0,
            "confidence": 75.0,
            "recommendation": "BUY",
            "signal_time": "2024-01-01T00:00:00",
            "status": "READY",
        }
        order = engine._execute_plan(plan, None)

        # Trading must NOT break despite notification failure
        assert order is not None
        assert order.status == "FILLED"
        assert "ETH/USDT" in engine.positions
        assert engine.positions["ETH/USDT"].status == "OPEN"

    def test_notification_only_once_per_buy(self, tmp_path: Any) -> None:
        """Each new BUY fill triggers exactly one notification."""
        from scripts.paper_trading_engine import PaperTradingEngine

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        mock_notifier = MagicMock()
        engine = PaperTradingEngine(notifier=mock_notifier)

        plan = {
            "symbol": "SOL/USDT",
            "entry_price": 50.0,
            "quantity": 10.0,
            "position_size_usdt": 500.0,
            "stop_loss": 47.5,
            "tp1": 52.5,
            "tp2": 55.0,
            "tp3": 57.5,
            "risk_amount": 25.0,
            "reward_amount": 25.0,
            "risk_reward": 1.0,
            "confidence": 70.0,
            "recommendation": "BUY",
            "signal_time": "2024-01-01T00:00:00",
            "status": "READY",
        }
        engine._execute_plan(plan, None)
        assert mock_notifier.notify_buy_opened.call_count == 1

    def test_no_notification_on_restart_for_already_notified(self, tmp_path: Any) -> None:
        """Restart recovery does NOT resend notifications for already-notified
        positions.  Test directly via _notify_existing_positions dedup."""
        from main import _notify_existing_positions
        from scripts.position_status import is_open

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        # Write positions.json with a position
        positions = {
            "positions": [
                {
                    "symbol": "BTC/USDT",
                    "entry_price": 100.0,
                    "quantity": 1.0,
                    "position_size_usdt": 100.0,
                    "stop_loss": 95.0,
                    "tp1": 105.0,
                    "status": "OPEN",
                },
            ]
        }
        with open("data/positions.json", "w") as f:
            json.dump(positions, f)

        # Write .notified_buys so this symbol is already notified
        with open("data/.notified_buys", "w") as f:
            f.write("BTC/USDT\n")

        mock_logger = MagicMock()
        mock_notifier = MagicMock()

        _notify_existing_positions(mock_logger, mock_notifier)

        # Should NOT call notify_buy_opened since BTC/USDT is in notified set
        mock_notifier.notify_buy_opened.assert_not_called()

    def test_notify_on_restart_for_not_yet_notified(self, tmp_path: Any) -> None:
        """Restart recovery sends notification for positions NOT yet notified."""
        from main import _notify_existing_positions

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        # Write positions.json with an open position (no .notified_buys file)
        positions = {
            "positions": [
                {
                    "symbol": "BTC/USDT",
                    "entry_price": 100.0,
                    "quantity": 1.0,
                    "position_size_usdt": 100.0,
                    "stop_loss": 95.0,
                    "tp1": 105.0,
                    "status": "OPEN",
                },
            ]
        }
        with open("data/positions.json", "w") as f:
            json.dump(positions, f)

        # The position must ALSO exist as OPEN in the authoritative paper
        # ledger — a positions.json-only entry is a simulated ghost that
        # never executed and must NOT produce a BUY_OPENED notification.
        with open("data/paper_state.json", "w") as f:
            json.dump({
                "balance": 9900.0,
                "positions": {
                    "BTC/USDT": {
                        "symbol": "BTC/USDT",
                        "status": "OPEN",
                        "quantity": 1.0,
                        "remaining_qty": 1.0,
                        "entry_price": 100.0,
                        "current_price": 100.0,
                    },
                },
            }, f)

        mock_logger = MagicMock()
        mock_notifier = MagicMock()

        _notify_existing_positions(mock_logger, mock_notifier)

        # Should send notification
        mock_notifier.notify_buy_opened.assert_called_once()


# ============================================================================
#  BUG #2: Equity includes open position market values
# ============================================================================

class TestEquityIncludesPositionValues:
    """Equity must include cash + market value of all open positions."""

    def test_accounting_reconcile_includes_position_value(self, tmp_path: Any, monkeypatch: Any) -> None:
        """accounting_reconcile computes correct_equity = cash + position_value + unrealized_pnl."""
        import scripts.accounting_reconcile as rec_mod

        # Setup: cash=1589.08, two open positions
        # USDE: position_size_usdt=6000, unrealized=-5.0
        # SAPIEN: position_size_usdt=2400, unrealized=-5.92
        # position_value = 6000 + 2400 = 8400
        # remaining_unrealized = -5.0 + -5.92 = -10.92
        # correct_equity = 1589.08 + 8400 + (-10.92) = 9978.16
        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 1589.08,
            "final_equity": 1589.08,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "total_return_pct": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
        with open(tmp_path / "paper_balance.json", "w") as f:
            json.dump(pb_data, f)

        pos_list = [
            {
                "symbol": "USDE/USDT",
                "position_size_usdt": 6000.0,
                "cost_basis": 6010.0,
                "unrealized_pnl": -5.0,
                "entry_price": 1.0,
                "current_price": 0.999,
                "quantity": 6000.0,
                "remaining_qty": 6000.0,
                "status": "OPEN",
                "closure_notified": False,
            },
            {
                "symbol": "SAPIEN/USDT",
                "position_size_usdt": 2400.0,
                "cost_basis": 2405.0,
                "unrealized_pnl": -5.92,
                "entry_price": 2.0,
                "current_price": 1.995,
                "quantity": 1200.0,
                "remaining_qty": 1200.0,
                "status": "OPEN",
                "closure_notified": False,
            },
        ]
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": pos_list}, f)

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        rec_mod.reconcile()

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        # position_market_value = 0.999*6000 + 1.995*1200 = 5994 + 2394 = 8388
        # equity = 1589.08 + 8388 = 9977.08
        assert pb["final_equity"] == pytest.approx(9977.08, abs=0.01)
        # Return = (9977.08 - 10000) / 10000 * 100 = -0.23%
        assert pb["total_return_pct"] == pytest.approx(-0.2292, abs=0.01)

    def test_metrics_manager_reads_correct_equity(self, tmp_path: Any) -> None:
        """MetricsManager reads the corrected equity from paper_balance.json."""
        from scripts.metrics_manager import MetricsManager

        # paper_balance.json with correct equity (position values included)
        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 1589.08,
            "final_equity": 9978.16,
            "realized_pnl": 0.0,
            "unrealized_pnl": -10.92,
            "net_pnl": -10.92,
            "total_return_pct": -0.22,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
        with open(tmp_path / "paper_balance.json", "w") as f:
            json.dump(pb_data, f)

        pos_list = [
            {
                "symbol": "USDE/USDT",
                "position_size_usdt": 6000.0,
                "cost_basis": 6010.0,
                "unrealized_pnl": -5.0,
                "entry_price": 1.0,
                "current_price": 0.999,
                "quantity": 6000.0,
                "remaining_qty": 6000.0,
                "status": "OPEN",
                "closure_notified": False,
            },
            {
                "symbol": "SAPIEN/USDT",
                "position_size_usdt": 2400.0,
                "cost_basis": 2405.0,
                "unrealized_pnl": -5.92,
                "entry_price": 2.0,
                "current_price": 1.995,
                "quantity": 1200.0,
                "remaining_qty": 1200.0,
                "status": "OPEN",
                "closure_notified": False,
            },
        ]
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": pos_list}, f)

        mgr = MetricsManager(data_dir=str(tmp_path))
        a = mgr.account()

        assert a.balance == pytest.approx(1589.08)
        # position_market_value = 0.999*6000 + 1.995*1200 = 5994 + 2394 = 8388
        assert a.position_value == pytest.approx(8388.0, abs=0.01)
        assert a.equity == pytest.approx(9977.08, abs=0.01)
        assert a.unrealized_pnl == pytest.approx(-10.92)
        # net_pnl = equity - initial_balance = 9977.08 - 10000 = -22.92
        assert a.net_pnl == pytest.approx(-22.92)
        assert a.total_return_pct == pytest.approx(-0.2292, abs=0.01)
        # Exposure = position_value / equity * 100
        expected_exposure = (8388.0 / 9977.08) * 100
        assert a.exposure_pct == pytest.approx(expected_exposure, abs=0.01)
        # Invariant: equity = balance + position_value
        assert a.equity == pytest.approx(a.balance + a.position_value)

    def test_equity_includes_multiple_positions(self, tmp_path: Any) -> None:
        """Equity correctly sums market values of multiple open positions."""
        from scripts.metrics_manager import MetricsManager

        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 5000.0,
            "final_equity": 15000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 1000.0,
            "net_pnl": 1000.0,
            "total_return_pct": 50.0,
        }
        with open(tmp_path / "paper_balance.json", "w") as f:
            json.dump(pb_data, f)

        pos_list = [
            {
                "symbol": "BTC/USDT",
                "position_size_usdt": 5000.0,
                "cost_basis": 5010.0,
                "unrealized_pnl": 600.0,
                "entry_price": 50000.0,
                "current_price": 56000.0,
                "quantity": 0.1,
                "remaining_qty": 0.1,
                "status": "OPEN",
            },
            {
                "symbol": "ETH/USDT",
                "position_size_usdt": 5000.0,
                "cost_basis": 5010.0,
                "unrealized_pnl": 400.0,
                "entry_price": 3000.0,
                "current_price": 3400.0,
                "quantity": 1.67,
                "remaining_qty": 1.67,
                "status": "OPEN",
            },
        ]
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": pos_list}, f)

        a = MetricsManager(data_dir=str(tmp_path)).account()
        # position_market_value = 56000*0.1 + 3400*1.67 = 5600 + 5678 = 11278
        assert a.position_value == pytest.approx(11278.0, abs=0.01)
        assert a.equity == pytest.approx(16278.0, abs=0.01)
        assert a.open_positions == 2

    def test_equity_zero_when_no_positions(self, tmp_path: Any) -> None:
        """With no open positions, equity == balance."""
        from scripts.metrics_manager import MetricsManager

        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 9500.0,
            "final_equity": 9500.0,
            "realized_pnl": -500.0,
            "unrealized_pnl": 0.0,
            "net_pnl": -500.0,
        }
        with open(tmp_path / "paper_balance.json", "w") as f:
            json.dump(pb_data, f)
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": []}, f)

        a = MetricsManager(data_dir=str(tmp_path)).account()
        assert a.equity == pytest.approx(9500.0)
        assert a.position_value == 0.0
        assert a.net_pnl == pytest.approx(-500.0)

    def test_equity_after_partial_closure(self, tmp_path: Any) -> None:
        """After closing one position, equity includes remaining position value."""
        from main import _update_paper_on_closure
        from scripts.position_status import is_open

        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        # Set up initial paper_balance.json
        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 5000.0,
            "final_equity": 15000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 1000.0,
            "net_pnl": 1000.0,
            "total_return_pct": 50.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
        with open("data/paper_balance.json", "w") as f:
            json.dump(pb_data, f)

        # Two open positions
        pos_list = [
            {
                "symbol": "BTC/USDT",
                "position_size_usdt": 5000.0,
                "cost_basis": 5010.0,
                "unrealized_pnl": 600.0,
                "entry_price": 50000.0,
                "current_price": 56000.0,
                "quantity": 0.1,
                "remaining_qty": 0.1,
                "status": "OPEN",
                "closure_notified": False,
                "tp1_sold": False,
                "tp2_sold": False,
                "tp3_sold": False,
                "opened_at": "2024-01-01T00:00:00",
                "realized_pnl": 0.0,
                "total_pnl": 600.0,
                "tp1": 0.0,
                "tp2": 0.0,
                "tp3": 0.0,
                "stop_loss": 0.0,
            },
            {
                "symbol": "ETH/USDT",
                "position_size_usdt": 3000.0,
                "cost_basis": 3010.0,
                "unrealized_pnl": 400.0,
                "entry_price": 3000.0,
                "current_price": 3400.0,
                "quantity": 1.0,
                "remaining_qty": 1.0,
                "status": "OPEN",
                "closure_notified": False,
                "tp1_sold": False,
                "tp2_sold": False,
                "tp3_sold": False,
                "opened_at": "2024-01-01T00:00:00",
                "realized_pnl": 0.0,
                "total_pnl": 400.0,
                "tp1": 0.0,
                "tp2": 0.0,
                "tp3": 0.0,
                "stop_loss": 0.0,
            },
        ]
        with open("data/positions.json", "w") as f:
            json.dump({"positions": pos_list}, f)

        # Simulate a monitored position object for BTC/USDT closure
        class FakePos:
            status = "CLOSED"
            entry_price = 50000.0
            quantity = 0.1
            remaining_qty = 0.0
            holding_hours = 48.0
            floating_pnl_pct = 12.0
            total_pnl = 600.0
            entry_time = "2024-01-01T00:00:00"

        mock_logger = MagicMock()
        pnl, new_balance = _update_paper_on_closure(
            mock_logger, "BTC/USDT", FakePos(), 56000.0, "Take Profit",
        )

        # Read paper_balance.json after closure
        pb = json.loads(open("data/paper_balance.json").read())
        # BTC closed with ~600 profit, sells at ~5600 total_proceeds
        # ETH still open with market_value = current_price * remaining_qty = 3400 * 1.0 = 3400
        # equity = final_balance + remaining_market_value
        # balance >= 5000 + total_proceeds_from_sale
        assert pb["final_balance"] > 5000.0  # proceeds added
        assert pb["final_equity"] > pb["final_balance"]  # equity > balance (ETH open)
        # Invariant: equity = balance + position_value
        assert pb["unrealized_pnl"] == pytest.approx(400.0, abs=0.01)  # ETH's unrealized

    def test_return_never_negative_84_percent(self, tmp_path: Any) -> None:
        """With positions worth ~8400 and cash ~1589, return must be near 0%,
        not -84% (the bug that excluded position values from equity)."""
        from scripts.metrics_manager import MetricsManager

        pb_data = {
            "initial_balance": 10000.0,
            "final_balance": 1589.08,
            "final_equity": 9978.16,
            "realized_pnl": 0.0,
            "unrealized_pnl": -10.92,
            "net_pnl": -10.92,
            "total_return_pct": -0.22,
        }
        with open(tmp_path / "paper_balance.json", "w") as f:
            json.dump(pb_data, f)

        pos_list = [
            {
                "symbol": "USDE/USDT",
                "position_size_usdt": 6000.0,
                "cost_basis": 6010.0,
                "unrealized_pnl": -5.0,
                "entry_price": 1.0,
                "current_price": 0.999,
                "quantity": 6000.0,
                "remaining_qty": 6000.0,
                "status": "OPEN",
                "closure_notified": False,
            },
            {
                "symbol": "SAPIEN/USDT",
                "position_size_usdt": 2400.0,
                "cost_basis": 2405.0,
                "unrealized_pnl": -5.92,
                "entry_price": 2.0,
                "current_price": 1.995,
                "quantity": 1200.0,
                "remaining_qty": 1200.0,
                "status": "OPEN",
                "closure_notified": False,
            },
        ]
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": pos_list}, f)

        a = MetricsManager(data_dir=str(tmp_path)).account()
        # Return must NOT be -84% (the bug)
        assert a.total_return_pct > -50.0  # Should be near 0%
        assert a.total_return_pct == pytest.approx(-0.22, abs=1.0)
