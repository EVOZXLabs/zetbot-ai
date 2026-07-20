"""Regression tests for the 2026-07 production incident:

    Telegram reported "0 open positions / 0% exposure" while
    positions.json contained real, live positions with status
    BREAKEVEN/TRAILING and remaining_qty > 0.

Root cause: several readers of positions.json only matched the literal
status string "OPEN" and silently dropped positions that had moved to
PARTIAL / TRAILING / BREAKEVEN after a partial take-profit, even though
those positions still hold real exposure (remaining_qty > 0).

These tests pin down the fix (scripts.position_status.OPEN_STATUSES /
is_open) across every consumer that reads positions.json, so a future
change can't silently reintroduce a narrower filter in just one place.

Note: since a later fix, MetricsManager.account() reads balance/equity/
PnL figures directly from paper_balance.json (the authoritative source
written by scripts.paper_trading_engine) rather than recomputing them
from positions.json's market prices. positions.json is only used to
count/identify which positions are open. The tests below set
paper_balance.json's final_equity/unrealized_pnl explicitly to match
each scenario's open positions, the same way the paper engine would
have written them.
"""

import json
import os
import sys
from typing import Any

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.metrics_manager import MetricsManager
from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES, is_open


# ============================================================================
#  Helpers
# ============================================================================

def _write_pb(tmp_path: Any, **overrides: Any) -> None:
    data = {
        "initial_balance": 10_000.0,
        "final_balance": 8_000.0,
        "final_equity": 10_000.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "net_pnl": 0.0,
        "total_return_pct": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        **overrides,
    }
    with open(tmp_path / "paper_balance.json", "w") as f:
        json.dump(data, f)


def _pos(status: str, symbol: str = "GMX/USDT", remaining_qty: float = 0.7,
          remaining_pct: float = 70.0, **extra: Any) -> dict[str, Any]:
    """Build a position dict in an arbitrary status, remaining_qty > 0."""
    return {
        "symbol": symbol,
        "entry_price": 10.0,
        "current_price": 11.0,
        "quantity": 1.0,
        "remaining_qty": remaining_qty,
        "remaining_pct": remaining_pct,
        "status": status,
        "floating_pnl": (11.0 - 10.0) * remaining_qty,
        "floating_pnl_pct": 10.0,
        **extra,
    }


def _write_positions(tmp_path: Any, positions: list[dict[str, Any]]) -> None:
    data = {
        "generated": "2026-07-19T00:00:00Z",
        "total_positions": len(positions),
        "active_count": sum(1 for p in positions if is_open(p.get("status"))),
        "closed_count": sum(1 for p in positions if p.get("status") in CLOSED_STATUSES),
        "positions": positions,
    }
    with open(tmp_path / "positions.json", "w") as f:
        json.dump(data, f)


# ============================================================================
#  scripts.position_status — the canonical vocabulary itself
# ============================================================================

class TestPositionStatusVocabulary:
    @pytest.mark.parametrize("status", ["OPEN", "PARTIAL", "TRAILING", "BREAKEVEN"])
    def test_open_like_statuses_are_open(self, status: str) -> None:
        assert is_open(status) is True
        assert status in OPEN_STATUSES

    @pytest.mark.parametrize("status", ["CLOSED", "STOPPED", "TIMEOUT"])
    def test_closed_like_statuses_are_not_open(self, status: str) -> None:
        assert is_open(status) is False
        assert status in CLOSED_STATUSES

    def test_none_and_unknown_status_is_not_open(self) -> None:
        assert is_open(None) is False
        assert is_open("SOMETHING_NEW") is False

    def test_vocabularies_do_not_overlap(self) -> None:
        assert OPEN_STATUSES.isdisjoint(CLOSED_STATUSES)


# ============================================================================
#  MetricsManager — the Telegram-facing single source of truth
# ============================================================================

class TestMetricsManagerBreakevenTrailing:
    def _mgr(self, tmp_path: Any) -> MetricsManager:
        return MetricsManager(data_dir=str(tmp_path))

    def test_breakeven_position_counts_as_open(self, tmp_path: Any) -> None:
        """Exact reproduction of the reported incident."""
        # Each _pos() default: market value = 11.0 × 0.7 = 7.7, unrealized = 0.7
        # Two positions -> position_value = 15.4, unrealized = 1.4
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=8_015.4,
            unrealized_pnl=1.4, net_pnl=1.4,
        )
        _write_positions(tmp_path, [
            _pos("BREAKEVEN", symbol="GMX/USDT"),
            _pos("BREAKEVEN", symbol="XLM/USDT"),
        ])

        mgr = self._mgr(tmp_path)
        a = mgr.account()

        assert mgr.open_positions_count() == 2
        assert len(mgr.open_positions()) == 2
        assert a.open_positions == 2
        # Exposure can never be 0% while positions exist.
        assert a.exposure_pct > 0.0
        assert a.position_value > 0.0

    def test_trailing_and_partial_also_count_as_open(self, tmp_path: Any) -> None:
        # Three positions at default market value 7.7 each -> 23.1 total
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=8_023.1,
            unrealized_pnl=2.1, net_pnl=2.1,
        )
        _write_positions(tmp_path, [
            _pos("TRAILING", symbol="BTC/USDT"),
            _pos("PARTIAL", symbol="ETH/USDT"),
            _pos("OPEN", symbol="SOL/USDT"),
        ])

        a = self._mgr(tmp_path).account()
        assert a.open_positions == 3

    def test_closed_positions_excluded(self, tmp_path: Any) -> None:
        _write_pb(
            tmp_path, final_balance=10_000.0, final_equity=10_000.0,
            unrealized_pnl=0.0, net_pnl=0.0,
        )
        _write_positions(tmp_path, [
            _pos("CLOSED", symbol="ATM/USDT", remaining_qty=0.0),
            _pos("STOPPED", symbol="SUN/USDT", remaining_qty=0.0),
            _pos("TIMEOUT", symbol="SKY/USDT", remaining_qty=0.0),
        ])

        a = self._mgr(tmp_path).account()
        assert a.open_positions == 0
        assert a.exposure_pct == 0.0
        assert a.position_value == 0.0

    def test_no_positions_no_exposure(self, tmp_path: Any) -> None:
        _write_pb(
            tmp_path, final_balance=10_000.0, final_equity=10_000.0,
            unrealized_pnl=0.0, net_pnl=0.0,
        )
        _write_positions(tmp_path, [])

        a = self._mgr(tmp_path).account()
        assert a.open_positions == 0
        assert a.exposure_pct == 0.0

    def test_mixed_open_and_closed(self, tmp_path: Any) -> None:
        # One BREAKEVEN position at default market value 7.7
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=8_007.7,
            unrealized_pnl=0.7, net_pnl=0.7,
        )
        _write_positions(tmp_path, [
            _pos("BREAKEVEN", symbol="GMX/USDT"),
            _pos("CLOSED", symbol="ATM/USDT", remaining_qty=0.0),
        ])

        a = self._mgr(tmp_path).account()
        assert a.open_positions == 1

    def test_runtime_invariant_exposure_nonzero_when_positions_exist(
        self, tmp_path: Any,
    ) -> None:
        """If positions > 0, exposure cannot be 0%."""
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=8_007.7,
            unrealized_pnl=0.7, net_pnl=0.7,
        )
        _write_positions(tmp_path, [_pos("BREAKEVEN")])

        a = self._mgr(tmp_path).account()
        if a.open_positions > 0:
            assert a.exposure_pct != 0.0

    def test_runtime_invariant_cash_never_exceeds_equity_with_positions(
        self, tmp_path: Any,
    ) -> None:
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=8_007.7,
            unrealized_pnl=0.7, net_pnl=0.7,
        )
        _write_positions(tmp_path, [_pos("TRAILING")])

        a = self._mgr(tmp_path).account()
        if a.open_positions > 0:
            assert a.balance <= a.equity


# ============================================================================
#  Cross-module consistency — every positions.json consumer must agree
# ============================================================================

class TestCrossModuleConsistency:
    """Every module that answers 'how many open positions are there?'
    from positions.json must return the same number for the same file.
    """

    def test_service_container_adapter_matches_metrics_manager(
        self, tmp_path: Any,
    ) -> None:
        from scripts.service_container import _PositionAdapter

        _write_positions(tmp_path, [
            _pos("BREAKEVEN", symbol="GMX/USDT"),
            _pos("TRAILING", symbol="XLM/USDT"),
            _pos("CLOSED", symbol="ATM/USDT", remaining_qty=0.0),
        ])
        _write_pb(tmp_path, final_balance=8_000.0)

        class _FakeConfig:
            data_dir = str(tmp_path)

        adapter = _PositionAdapter(_FakeConfig())
        mgr = MetricsManager(data_dir=str(tmp_path))

        assert len(adapter.get_open_positions()) == mgr.open_positions_count() == 2

    def test_health_snapshot_matches_metrics_manager(self, tmp_path: Any) -> None:
        import scripts.health as health_module

        _write_positions(tmp_path, [
            _pos("BREAKEVEN", symbol="GMX/USDT"),
            _pos("PARTIAL", symbol="XLM/USDT"),
        ])
        _write_pb(tmp_path, final_balance=8_000.0)

        positions = health_module._read_json(
            f"{tmp_path}/positions.json"
        ).get("positions", [])
        open_count = sum(
            1 for p in positions
            if health_module.is_open(p.get("status"))
        )

        mgr = MetricsManager(data_dir=str(tmp_path))
        assert open_count == mgr.open_positions_count() == 2
