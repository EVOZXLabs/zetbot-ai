"""Regression tests for legacy-quote position purge (BTC/USDT on IDR account).

The bug: ``ExecutionPipeline.reconcile_position()`` purged a mismatched-quote
position from ``positions.json`` but returned the original position dict.
Callers (``reconcile_exit``, ``_monitor_positions``) then saved that dict
back, recreating the record every ~60 s.

These tests prove the fix: after a purge, the position must stay gone and
``reconcile_position`` must return ``None`` so callers skip the save.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from scripts.execution_pipeline import ExecutionPipeline
from scripts.execution_provider import PaperExecutionProvider
from scripts.position_status import OPEN_STATUSES


def _write_positions(path: str, positions: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"positions": positions}, f, indent=2, default=str)


def _load_positions(path: str) -> list[dict[str, Any]]:
    try:
        with open(path) as f:
            return json.load(f).get("positions", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


class _FakeProvider(PaperExecutionProvider):
    """Paper provider that records calls and returns a fixed price."""

    def __init__(self) -> None:
        self.mode = "PAPER"
        self.last_execute_sell: Any = None

    def execute_sell(self, request: Any) -> Any:  # noqa: D401
        self.last_execute_sell = request
        from scripts.execution_provider import OrderResult
        return OrderResult(
            order_id="sell123",
            trace_id=request.trace_id,
            execution_id="ex1",
            status="FILLED",
            symbol=request.symbol,
            side="SELL",
            type=request.type,
            amount=request.amount,
            filled_amount=request.amount,
            filled_price=50000.0,
            fee=0.0,
            cost=request.amount * 50000.0,
            latency_ms=1.0,
            retries=0,
            executor="fake",
            exchange="fake",
            mode="PAPER",
            timestamp="2024-01-01T00:00:00+00:00",
        )


class TestLegacyQuotePurge:
    """reconcile_position must purge mismatched-quote positions and return None."""

    def test_purge_returns_none_and_removes_position(self, tmp_path: Any) -> None:
        pos_path = str(tmp_path / "positions.json")
        btc = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 50000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "stop_loss": 49000.0,
            "tp1": 51500.0,
            "tp2": 53000.0,
            "tp3": 55000.0,
        }
        _write_positions(pos_path, [btc])

        pipeline = ExecutionPipeline(_FakeProvider(), quote_currency="IDR", positions_path=pos_path)
        result = pipeline.reconcile_position(
            "BTC/USDT", 51000.0, btc, plan={},
        )

        assert result is None
        remaining = _load_positions(pos_path)
        assert not any(p.get("symbol") == "BTC/USDT" for p in remaining)

    def test_multiple_reconciles_do_not_recreate_position(self, tmp_path: Any) -> None:
        pos_path = str(tmp_path / "positions.json")
        btc = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 50000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "stop_loss": 49000.0,
            "tp1": 51500.0,
        }
        _write_positions(pos_path, [btc])

        pipeline = ExecutionPipeline(_FakeProvider(), quote_currency="IDR", positions_path=pos_path)
        for _ in range(5):
            result = pipeline.reconcile_position(
                "BTC/USDT", 51000.0, btc, plan={},
            )
            assert result is None

        remaining = _load_positions(pos_path)
        assert not any(p.get("symbol") == "BTC/USDT" for p in remaining)

    def test_matching_quote_is_not_purged(self, tmp_path: Any) -> None:
        pos_path = str(tmp_path / "positions.json")
        koma = {
            "symbol": "KOMA/IDR",
            "status": "OPEN",
            "entry_price": 250.0,
            "quantity": 100.0,
            "remaining_qty": 100.0,
            "stop_loss": 240.0,
            "tp1": 260.0,
        }
        _write_positions(pos_path, [koma])

        pipeline = ExecutionPipeline(_FakeProvider(), quote_currency="IDR", positions_path=pos_path)
        result = pipeline.reconcile_position(
            "KOMA/IDR", 255.0, koma, plan={},
        )

        assert result is not None
        assert result.get("status") in OPEN_STATUSES
        remaining = _load_positions(pos_path)
        assert any(p.get("symbol") == "KOMA/IDR" for p in remaining)

    def test_purge_is_best_effort_and_never_raises(self, tmp_path: Any) -> None:
        """Even if positions.json is unreadable, reconcile_position must not raise."""
        pos_path = str(tmp_path / "positions.json")
        btc = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 50000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "stop_loss": 49000.0,
            "tp1": 51500.0,
        }
        _write_positions(pos_path, [btc])

        pipeline = ExecutionPipeline(_FakeProvider(), quote_currency="IDR", positions_path=pos_path)
        # Corrupt the file mid-run by writing invalid JSON
        with open(pos_path, "w") as f:
            f.write("not json")

        result = pipeline.reconcile_position(
            "BTC/USDT", 51000.0, btc, plan={},
        )
        assert result is None

    def test_no_quote_configured_does_not_purge(self, tmp_path: Any) -> None:
        pos_path = str(tmp_path / "positions.json")
        btc = {
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_price": 50000.0,
            "quantity": 0.1,
            "remaining_qty": 0.1,
            "stop_loss": 49000.0,
            "tp1": 51500.0,
        }
        _write_positions(pos_path, [btc])

        pipeline = ExecutionPipeline(_FakeProvider(), quote_currency="", positions_path=pos_path)
        result = pipeline.reconcile_position(
            "BTC/USDT", 51000.0, btc, plan={},
        )

        assert result is not None
        remaining = _load_positions(pos_path)
        assert any(p.get("symbol") == "BTC/USDT" for p in remaining)
