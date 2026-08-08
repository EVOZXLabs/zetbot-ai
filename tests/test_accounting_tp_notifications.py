"""Regression tests for accounting consistency and TP/close notifications.

Covers:
- _persist_paper_state writes all derived metrics via canonical snapshot
- ExecutionPipeline sends exactly one Telegram notification per TP level
- Manual /sell sends a position-closed notification
- Accounting invariants hold after partial TP sells
- paper_balance.json fields never drift from canonical computation
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================================
#  Helpers
# ============================================================================

def _write_pb(tmp_path: Any, **overrides: Any) -> dict[str, Any]:
    data = {
        "initial_balance": 10_000.0,
        "final_balance": 10_000.0,
        "final_equity": 10_000.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "net_pnl": 0.0,
        "total_return_pct": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        **overrides,
    }
    with open(tmp_path / "paper_balance.json", "w") as f:
        json.dump(data, f)
    return data


def _open_pos(
    symbol: str = "BTC/USDT",
    entry_price: float = 100_000.0,
    current_price: float = 105_000.0,
    quantity: float = 0.1,
    remaining_qty: float = 0.1,
    tp1: float = 110_000.0,
    tp2: float = 115_000.0,
    tp3: float = 120_000.0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "order_id": "paper-1",
        "quantity": quantity,
        "remaining_qty": remaining_qty,
        "entry_price": entry_price,
        "current_price": current_price,
        "unrealized_pnl": (current_price - entry_price) * remaining_qty,
        "realized_pnl": 0.0,
        "total_pnl": (current_price - entry_price) * remaining_qty,
        "cost_basis": entry_price * quantity,
        "status": "OPEN",
        "tp1_sold": False,
        "tp2_sold": False,
        "tp3_sold": False,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "signal_time": "2026-01-01T00:00:00+00:00",
        "closure_notified": False,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "stop_loss": 95_000.0,
        "position_size_usdt": entry_price * quantity,
        "floating_pnl": (current_price - entry_price) * remaining_qty,
        "floating_pnl_pct": ((current_price / entry_price) - 1) * 100,
        **extra,
    }


# ============================================================================
#  PART A — _persist_paper_state writes all derived metrics
# ============================================================================

class TestPersistPaperStateWritesAllMetrics:
    """Pipeline._persist_paper_state must write every derived field."""

    def test_all_fields_written(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        _write_pb(
            tmp_path,
            initial_balance=10_000.0,
            final_balance=9_500.0,
        )

        from scripts.pipeline import Pipeline
        from scripts.app_config import AppConfig
        from scripts.metrics_manager import MetricsManager

        cfg = AppConfig(
            exchange="binance",
            quote_currency="USDT",
            account_balance=10_000.0,
            data_dir=str(tmp_path),
        )
        logger = MagicMock()
        pipeline = Pipeline(cfg, logger)

        class FakeVP:
            status = "OPEN"
            current_price = 105_000.0
            remaining_qty = 0.1
            realized_pnl = 200.0
            total_pnl = 700.0
            unrealized_pnl = 500.0

        class FakeProvider:
            def get_balance(self) -> float:
                return 9_500.0

            positions = {"BTC/USDT": FakeVP()}

        pipeline._persist_paper_state(FakeProvider())

        with open(tmp_path / "data" / "paper_balance.json") as f:
            pb = json.load(f)

        assert "final_equity" in pb
        assert "net_pnl" in pb
        assert "total_return_pct" in pb
        assert "realized_pnl" in pb
        assert "unrealized_pnl" in pb
        assert "total_trades" in pb
        assert "winning_trades" in pb
        assert "losing_trades" in pb
        assert "win_rate" in pb
        assert "profit_factor" in pb
        assert "gross_profit" in pb
        assert "gross_loss" in pb

        snapshot = MetricsManager.compute_snapshot(
            cash=pb["final_balance"],
            realized_pnl=pb.get("realized_pnl", 0.0),
            initial_balance=pb.get("initial_balance", 10_000.0),
            open_positions=[
                {"current_price": 105_000.0, "remaining_qty": 0.1}
            ],
            total_trades=pb.get("total_trades", 0),
            winning_trades=pb.get("winning_trades", 0),
            losing_trades=pb.get("losing_trades", 0),
            win_rate=pb.get("win_rate", 0.0),
            profit_factor=pb.get("profit_factor", 0.0),
            gross_profit=pb.get("gross_profit", 0.0),
            gross_loss=pb.get("gross_loss", 0.0),
        )
        assert pb["final_equity"] == pytest.approx(snapshot.equity, abs=0.01)
        assert pb["net_pnl"] == pytest.approx(snapshot.net_pnl, abs=0.01)
        assert pb["total_return_pct"] == pytest.approx(snapshot.total_return_pct, abs=0.01)


# ============================================================================
#  PART D — TP notifications
# ============================================================================

class TestTPNotifications:
    """ExecutionPipeline must send exactly one Telegram notification per TP."""

    def test_tp1_sends_notification(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        from scripts.execution_pipeline import ExecutionPipeline
        from scripts.execution_provider import OrderResult

        notifier = MagicMock()
        provider = MagicMock()
        provider.execute_sell.return_value = OrderResult(
            order_id="sell-1", status="FILLED", symbol="BTC/USDT",
            side="SELL", amount=0.03, filled_amount=0.03,
            filled_price=110_000.0, cost=3_300.0,
        )

        pipeline = ExecutionPipeline(
            provider, quote_currency="USDT", notifier=notifier,
        )
        position = {
            "symbol": "BTC/USDT",
            "entry_price": 100_000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "cost_basis": 10_000.0,
            "tp1": 110_000.0,
            "tp2": 115_000.0,
            "tp3": 120_000.0,
            "stop_loss": 95_000.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        result = pipeline.reconcile_position(
            "BTC/USDT", 110_000.0, position,
        )
        assert result is not None
        assert result.get("tp1_hit") is True
        assert notifier.notify_take_profit.call_count == 1
        call = notifier.notify_take_profit.call_args
        assert call.kwargs["symbol"] == "BTC/USDT"
        assert call.kwargs["entry_price"] == 100_000.0
        assert call.kwargs["exit_price"] == 110_000.0

    def test_tp2_sends_notification(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        from scripts.execution_pipeline import ExecutionPipeline
        from scripts.execution_provider import OrderResult

        notifier = MagicMock()
        provider = MagicMock()
        provider.execute_sell.return_value = OrderResult(
            order_id="sell-2", status="FILLED", symbol="BTC/USDT",
            side="SELL", amount=0.03, filled_amount=0.03,
            filled_price=115_000.0, cost=3_450.0,
        )

        pipeline = ExecutionPipeline(
            provider, quote_currency="USDT", notifier=notifier,
        )
        position = {
            "symbol": "BTC/USDT",
            "entry_price": 100_000.0,
            "quantity": 0.1,
            "remaining_qty": 0.07,
            "cost_basis": 10_000.0,
            "tp1": 110_000.0,
            "tp2": 115_000.0,
            "tp3": 120_000.0,
            "stop_loss": 95_000.0,
            "tp1_hit": True,
            "tp2_hit": False,
            "tp3_hit": False,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        result = pipeline.reconcile_position(
            "BTC/USDT", 115_000.0, position,
        )
        assert result is not None
        assert result.get("tp2_hit") is True
        assert notifier.notify_take_profit.call_count == 1

    def test_tp3_sends_notification(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        from scripts.execution_pipeline import ExecutionPipeline
        from scripts.execution_provider import OrderResult

        notifier = MagicMock()
        provider = MagicMock()
        provider.execute_sell.return_value = OrderResult(
            order_id="sell-3", status="FILLED", symbol="BTC/USDT",
            side="SELL", amount=0.04, filled_amount=0.04,
            filled_price=120_000.0, cost=4_800.0,
        )

        pipeline = ExecutionPipeline(
            provider, quote_currency="USDT", notifier=notifier,
        )
        position = {
            "symbol": "BTC/USDT",
            "entry_price": 100_000.0,
            "quantity": 0.1,
            "remaining_qty": 0.04,
            "cost_basis": 10_000.0,
            "tp1": 110_000.0,
            "tp2": 115_000.0,
            "tp3": 120_000.0,
            "stop_loss": 95_000.0,
            "tp1_hit": True,
            "tp2_hit": True,
            "tp3_hit": False,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        result = pipeline.reconcile_position(
            "BTC/USDT", 120_000.0, position,
        )
        assert result is not None
        assert result.get("tp3_hit") is True
        assert notifier.notify_take_profit.call_count == 1

    def test_no_duplicate_tp_notification(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        from scripts.execution_pipeline import ExecutionPipeline
        from scripts.execution_provider import OrderResult

        notifier = MagicMock()
        provider = MagicMock()
        provider.execute_sell.return_value = OrderResult(
            order_id="sell-1", status="FILLED", symbol="BTC/USDT",
            side="SELL", amount=0.03, filled_amount=0.03,
            filled_price=110_000.0, cost=3_300.0,
        )

        pipeline = ExecutionPipeline(
            provider, quote_currency="USDT", notifier=notifier,
        )
        position = {
            "symbol": "BTC/USDT",
            "entry_price": 100_000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "cost_basis": 10_000.0,
            "tp1": 110_000.0,
            "tp2": 115_000.0,
            "tp3": 120_000.0,
            "stop_loss": 95_000.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        result = pipeline.reconcile_position(
            "BTC/USDT", 110_000.0, position,
        )
        assert result is not None
        assert notifier.notify_take_profit.call_count == 1

        # Second call at same price must NOT send another notification
        result2 = pipeline.reconcile_position(
            "BTC/USDT", 110_000.0, result,
        )
        assert result2 is not None
        assert notifier.notify_take_profit.call_count == 1


# ============================================================================
#  PART A — Accounting after partial TP
# ============================================================================

class TestAccountingAfterPartialTP:
    """Accounting must remain consistent after partial TP sells."""

    def test_partial_tp_accounting(self, tmp_path: Any) -> None:
        from scripts.metrics_manager import MetricsManager

        initial_balance = 10_000.0
        balance_after_buy = 9_500.0
        tp1_proceeds = 3_300.0
        balance_after_tp1 = balance_after_buy + tp1_proceeds

        # After TP1: 30% sold, 70% remains
        remaining_qty = 0.07
        current_price = 110_000.0
        cost_remaining = 10_000.0 * (remaining_qty / 0.1)
        unrealized = current_price * remaining_qty - cost_remaining
        realized = tp1_proceeds - (10_000.0 * 0.3)
        position_market_value = current_price * remaining_qty
        equity = balance_after_tp1 + position_market_value

        _write_pb(
            tmp_path,
            initial_balance=initial_balance,
            final_balance=balance_after_tp1,
            final_equity=equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            net_pnl=equity - initial_balance,
        )
        pos = _open_pos(
            quantity=0.1, remaining_qty=remaining_qty,
            current_price=current_price,
        )
        with open(tmp_path / "positions.json", "w") as f:
            json.dump({"positions": [pos]}, f)

        mgr = MetricsManager(data_dir=str(tmp_path))
        snap = mgr.account()

        assert snap.equity == pytest.approx(equity, abs=0.01)
        # net_pnl is account-centric: equity - initial_balance
        assert snap.net_pnl == pytest.approx(equity - initial_balance, abs=0.01)
        assert snap.equity == pytest.approx(snap.balance + snap.position_value)
        assert snap.total_return_pct == pytest.approx(
            ((equity - initial_balance) / initial_balance) * 100, abs=0.01
        )


# ============================================================================
#  PART D — Manual /sell notification
# ============================================================================

class TestManualSellNotification:
    """Telegram /sell must send a POSITION_CLOSED notification."""

    def test_sell_command_sends_notification(self, tmp_path: Any) -> None:
        os.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        with open("data/positions.json", "w") as f:
            json.dump({
                "positions": [
                    _open_pos(
                        symbol="BTC/USDT",
                        quantity=0.1,
                        remaining_qty=0.1,
                        current_price=110_000.0,
                        total_pnl=1_000.0,
                    )
                ]
            }, f)

        from telegram.commands.sell import SellCommand
        from telegram.context import CommandContext

        notifier = MagicMock()
        notifier.notify_position_closed.return_value = True

        class FakeOrderResult:
            status = "FILLED"
            filled_amount = 0.1
            filled_price = 110_000.0
            error = None
            executor = "test"
            amount = 0.1

        services = MagicMock()
        services.order.execute.return_value = FakeOrderResult()
        services.position.get_open_positions.return_value = [
            _open_pos(symbol="BTC/USDT", quantity=0.1, remaining_qty=0.1,
                      current_price=110_000.0, total_pnl=1_000.0)
        ]
        services.position.get_all.return_value = services.position.get_open_positions.return_value
        services.config.quote_currency = "USDT"
        services.notification.notify_close = notifier.notify_position_closed

        ctx = MagicMock()
        ctx.services = services
        ctx.config = MagicMock()
        ctx.config.quote_currency = "USDT"

        cmd = SellCommand()
        cmd.execute(ctx, "BTC/USDT")

        assert notifier.notify_position_closed.call_count >= 1
