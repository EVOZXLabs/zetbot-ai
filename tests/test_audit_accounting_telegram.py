"""Regression tests for the AUDIT ACCOUNTING + TELEGRAM CONSISTENCY milestone.

Covers the four root causes found during the audit:

- RC1  Currency label mismatches: Telegram close/TP/SL notifications and the
       ``/positions`` paper cards must display the ACCOUNT quote currency
       (e.g. IDR on Indodax), never the symbol's own suffix (``BTC/USDT``
       must NOT render PnL as USDT on an IDR account).
- RC2  PnL mismatch: ``_update_paper_on_closure`` must report the engine's
       finalized ledger pnl (``paper_state.json`` ``total_pnl``) when the
       closure was already handled, not the reconciled ``positions.json``
       value.
- RC3  Restored legacy positions: ``reconcile_position`` must never TP/SL a
       position whose symbol quote does not match the account quote.
- RC4  Held duration: close notifications must derive the holding time from
       ``entry_time``/``opened_at`` (``reconcile_position`` never persists
       ``holding_hours``, so the old code always reported "0s").
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
#  RC4 — Held duration in close notifications
# ===========================================================================


class TestHoldingTimeFromPosition:
    """``_holding_time_from_position`` must report real durations, not 0s."""

    @pytest.mark.parametrize(
        "key",
        ["entry_time", "opened_at"],
    )
    def test_derives_from_entry_timestamp(self, key: str) -> None:
        from main import _holding_time_from_position

        pos = {key: "2024-01-01T00:00:00+00:00"}
        holding = _holding_time_from_position(pos)
        # Aug 2026 test-run: ~95 days elapsed — never "0s" like the bug.
        assert holding.days >= 90

    def test_dict_entry_time_with_plus_offset(self) -> None:
        from main import _holding_time_from_position

        pos = {"entry_time": "2026-07-01T10:00:00+00:00"}
        holding = _holding_time_from_position(pos)
        assert holding.total_seconds() > 0

    def test_object_with_entry_time_attr(self) -> None:
        from main import _holding_time_from_position

        pos = SimpleNamespace(entry_time="2026-07-01T00:00:00+00:00")
        holding = _holding_time_from_position(pos)
        assert holding.total_seconds() > 0

    def test_fallback_holding_hours(self) -> None:
        from main import _holding_time_from_position

        holding = _holding_time_from_position({"holding_hours": 100})
        assert holding == timedelta(hours=100)

    def test_no_timestamp_no_hours_returns_zero(self) -> None:
        from main import _holding_time_from_position

        assert _holding_time_from_position({}) == timedelta()
        assert _holding_time_from_position({"holding_hours": 0}) == timedelta()

    def test_invalid_timestamp_falls_back(self) -> None:
        from main import _holding_time_from_position

        holding = _holding_time_from_position({"entry_time": "not-a-date", "holding_hours": 5})
        assert holding == timedelta(hours=5)


# ===========================================================================
#  RC2 — Closure already handled by the engine: ledger pnl is authoritative
# ===========================================================================


class TestUpdatePaperOnClosureLedgerPnl:
    """When paper_state.json already shows the position CLOSED, the
    notification pnl must be the engine's finalized ledger pnl, not the
    reconciled positions.json value."""

    def _setup(self, tmp_path: Any) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        paper_state = {
            "balance": 20822.49,
            "positions": {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "status": "CLOSED",
                    "remaining_qty": 0.0,
                    "realized_pnl": 1487.92,
                    "total_pnl": 1487.92,
                    "opened_at": "2024-01-01T00:00:00+00:00",
                }
            },
        }
        with open(data_dir / "paper_state.json", "w") as f:
            json.dump(paper_state, f, indent=2)

        paper_balance = {
            "initial_balance": 10000.0,
            "final_balance": 20822.50,
            "final_equity": 20822.50,
            "total_trades": 1,
            "winning_trades": 1,
            "losing_trades": 0,
            "win_rate": 100.0,
            "realized_pnl": 1487.92,
            "unrealized_pnl": 0.0,
            "net_pnl": 1487.92,
        }
        with open(data_dir / "paper_balance.json", "w") as f:
            json.dump(paper_balance, f, indent=2)

        positions = {
            "positions": [
                {
                    "symbol": "BTC/USDT",
                    "status": "OPEN",
                    "quantity": 0.1,
                    "remaining_qty": 0.1,
                    "entry_price": 50000.0,
                    "cost_basis": 5000.0,
                    "total_pnl": 328.07,
                    "realized_pnl": 328.07,
                    "opened_at": "2024-01-01T00:00:00+00:00",
                }
            ],
            "total_positions": 1,
            "active_count": 1,
            "closed_count": 0,
        }
        with open(data_dir / "positions.json", "w") as f:
            json.dump(positions, f, indent=2)

        os.chdir(tmp_path)

    def test_uses_engine_ledger_pnl(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _update_paper_on_closure

        reconciled = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "entry_price": 50000.0,
            "cost_basis": 5000.0,
            "total_pnl": 328.07,
            "realized_pnl": 328.07,
            "opened_at": "2024-01-01T00:00:00+00:00",
        }
        pnl, balance = _update_paper_on_closure(
            MagicMock(), "BTC/USDT", reconciled,
            exit_price=65028.77, exit_reason="Take Profit",
        )
        # Engine's finalized ledger pnl wins over the reconciled value.
        assert pnl == pytest.approx(1487.92)
        # Already handled -> no second credit to the wallet.
        assert balance == pytest.approx(20822.50)

    def test_no_double_credit_to_balance(self, tmp_path: Any) -> None:
        self._setup(tmp_path)
        from main import _update_paper_on_closure

        reconciled = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "entry_price": 50000.0,
            "cost_basis": 5000.0,
            "total_pnl": 328.07,
        }
        _update_paper_on_closure(
            MagicMock(), "BTC/USDT", reconciled,
            exit_price=65028.77, exit_reason="Take Profit",
        )

        with open("data/paper_balance.json") as f:
            pb = json.load(f)
        assert pb["final_balance"] == pytest.approx(20822.50)
        assert pb["realized_pnl"] == pytest.approx(1487.92)


# ===========================================================================
#  RC3 — Restored/legacy positions with a mismatched quote are left alone
# ===========================================================================


class TestReconcilePositionCurrencyGuard:
    """``reconcile_position`` must skip TP/SL for a symbol whose quote does
    not match the account quote (e.g. restored ``BTC/USDT`` on an IDR
    account)."""

    def _position(self) -> dict[str, Any]:
        return {
            "symbol": "BTC/USDT",
            "status": "OPEN",
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

    def test_mismatched_symbol_quote_left_untouched(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        notifier = MagicMock()
        provider = MagicMock()
        pipeline = ExecutionPipeline(
            provider, quote_currency="IDR", notifier=notifier,
        )
        position = self._position()
        # current_price way past TP1 — would trigger if not for the guard.
        result = pipeline.reconcile_position("BTC/USDT", 120_000.0, position)

        assert result == position
        assert result.get("tp1_hit") is False
        assert result.get("remaining_qty") == 0.1
        assert provider.execute_sell.call_count == 0
        assert notifier.notify_take_profit.call_count == 0

    def test_mismatched_symbol_notifier_not_called(self) -> None:
        from scripts.execution_pipeline import ExecutionPipeline

        notifier = MagicMock()
        pipeline = ExecutionPipeline(
            MagicMock(), quote_currency="IDR", notifier=notifier,
        )
        position = self._position()
        pipeline.reconcile_position("BTC/USDT", 120_000.0, position)
        assert notifier.notify_take_profit.call_count == 0
        assert notifier.notify_stop_loss.call_count == 0

    def test_matching_symbol_still_reconciles(self, tmp_path: Any) -> None:
        """A BTC/USDT symbol on a USDT account must still hit TP1."""
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
        result = pipeline.reconcile_position("BTC/USDT", 110_000.0, self._position())
        assert result is not None
        assert result.get("tp1_hit") is True
        assert notifier.notify_take_profit.call_count == 1


# ===========================================================================
#  RC1 — /positions paper cards use the account quote currency
# ===========================================================================


class TestPositionsCommandCurrency:
    """``/positions`` paper cards must render PnL in the account quote
    currency (IDR), not the symbol's ``/USDT`` suffix."""

    def _ctx(self, quote: str) -> SimpleNamespace:
        positions = {
            "positions": [
                {
                    "symbol": "BTC/USDT",
                    "status": "OPEN",
                    "quantity": 0.1,
                    "remaining_qty": 0.1,
                    "entry_price": 50000.0,
                    "current_price": 65028.77,
                    "cost_basis": 5000.0,
                    "opened_at": "2024-01-01T00:00:00+00:00",
                }
            ],
            "total_positions": 1,
            "active_count": 1,
            "closed_count": 0,
        }
        return SimpleNamespace(
            services=None,
            config=SimpleNamespace(quote_currency=quote),
            read_json=lambda _name: positions,
        )

    def test_idr_account_shows_idr_pnl(self) -> None:
        from telegram.commands.positions import PositionsCommand

        text = PositionsCommand().execute(self._ctx("IDR"), "")
        # PnL = 6502.877 - 5000 = +1,502.88 — rendered in IDR, never USDT.
        assert "+1,502.88 IDR" in text
        assert "+1,502.88 USDT" not in text

    def test_usdt_account_still_shows_usdt(self) -> None:
        from telegram.commands.positions import PositionsCommand

        text = PositionsCommand().execute(self._ctx("USDT"), "")
        assert "+1,502.88 USDT" in text
        assert "+1,502.88 IDR" not in text
