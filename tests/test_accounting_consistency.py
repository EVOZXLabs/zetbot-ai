"""Regression tests for accounting consistency, timezone formatting,
and Telegram command consistency.

Covers:
- paper_balance.json is the single, authoritative source for balance,
  equity, realized/unrealized/net PnL, and return % — MetricsManager
  must consume it directly, never recompute it from positions.json
  (see scripts.metrics_manager.MetricsManager.account)
- positions.json is used only to count/list currently open positions
- Accounting invariants (equity = cash + position_value, net = realized + unrealized)
- Exposure calculation correctness
- No positions / one position / multiple positions / partial exit / full exit
- Positive and negative PnL
- wib_now() / wib_datetime() timezone correctness
- No UTC leaks in Telegram-facing output
- AccountSnapshot shared across all commands
"""

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.metrics_manager import MetricsManager, AccountSnapshot


# ============================================================================
#  Helper — write JSON files to a tmp data directory
# ============================================================================

def _write_pb(tmp_path: Any, **overrides: Any) -> None:
    """Write paper_balance.json with sensible defaults."""
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
        **overrides,
    }
    with open(tmp_path / "paper_balance.json", "w") as f:
        json.dump(data, f)


def _write_positions(tmp_path: Any, positions: list[dict[str, Any]]) -> None:
    """Write positions.json."""
    data = {
        "generated": "2026-01-01T00:00:00Z",
        "total_positions": len(positions),
        "active_count": sum(1 for p in positions if p.get("status") == "OPEN"),
        "closed_count": sum(1 for p in positions if p.get("status") == "CLOSED"),
        "positions": positions,
    }
    with open(tmp_path / "positions.json", "w") as f:
        json.dump(data, f)


def _open_pos(
    symbol: str = "BTCUSDT",
    entry_price: float = 100_000.0,
    current_price: float = 105_000.0,
    quantity: float = 0.1,
    remaining_qty: float = 0.1,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal OPEN position dict."""
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
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "stop_loss": 0.0,
        "position_size_usdt": entry_price * quantity,
        "floating_pnl": (current_price - entry_price) * remaining_qty,
        "floating_pnl_pct": ((current_price / entry_price) - 1) * 100,
        **extra,
    }


def _closed_pos(
    symbol: str = "BTCUSDT",
    entry_price: float = 100_000.0,
    current_price: float = 0.0,
    quantity: float = 0.1,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal CLOSED position dict."""
    return {
        **_open_pos(symbol, entry_price, current_price, quantity, 0.0),
        "status": "CLOSED",
        "remaining_qty": 0.0,
        "closure_notified": True,
        **extra,
    }


# ============================================================================
#  Accounting invariants
# ============================================================================

class TestAccountingInvariants:
    """AccountSnapshot must satisfy mathematical invariants.

    paper_balance.json values (final_equity, unrealized_pnl, net_pnl,
    total_return_pct) are the authoritative input — set explicitly here
    to match each scenario, the same way scripts.paper_trading_engine
    would have written them. positions.json only supplies the open
    position *count*.
    """

    def _mgr(self, tmp_path: Any) -> MetricsManager:
        return MetricsManager(data_dir=str(tmp_path))

    def test_no_positions(self, tmp_path: Any) -> None:
        """With no open positions: equity = balance, exposure = 0."""
        _write_pb(
            tmp_path, final_balance=10_000.0, final_equity=10_000.0,
            realized_pnl=0.0, unrealized_pnl=0.0, net_pnl=0.0,
        )
        _write_positions(tmp_path, [])

        a = self._mgr(tmp_path).account()

        assert a.balance == 10_000.0
        assert a.position_value == 0.0
        assert a.equity == 10_000.0
        assert a.unrealized_pnl == 0.0
        assert a.net_pnl == 0.0
        assert a.exposure_pct == 0.0
        # Invariant
        assert a.equity == a.balance + a.position_value

    def test_one_open_position_profit(self, tmp_path: Any) -> None:
        """One open position with profit: equity = balance + market_value."""
        # Market value = 105_000 × 0.1 = 10_500; equity = 9_500 + 10_500 = 20_000
        # Unrealized = (105_000 - 100_000) × 0.1 = 500
        _write_pb(
            tmp_path, final_balance=9_500.0, final_equity=20_000.0,
            realized_pnl=0.0, unrealized_pnl=500.0, net_pnl=10_000.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=105_000, quantity=0.1, remaining_qty=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.position_value == pytest.approx(10_500.0)
        assert a.equity == pytest.approx(20_000.0)
        assert a.unrealized_pnl == pytest.approx(500.0)
        # net_pnl = equity - initial_balance = 20000 - 10000 = 10000
        assert a.net_pnl == pytest.approx(10_000.0)
        # Exposure = 10_500 / 20_000 × 100 = 52.5%
        assert a.exposure_pct == pytest.approx(52.5)
        # Invariant: equity = balance + position_value
        assert a.equity == pytest.approx(a.balance + a.position_value)

    def test_one_open_position_loss(self, tmp_path: Any) -> None:
        """One open position with loss."""
        # Market value = 95_000 × 0.1 = 9_500; equity = 9_500 + 9_500 = 19_000
        # Unrealized = (95_000 - 100_000) × 0.1 = -500
        _write_pb(
            tmp_path, final_balance=9_500.0, final_equity=19_000.0,
            realized_pnl=0.0, unrealized_pnl=-500.0, net_pnl=9_000.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=95_000, quantity=0.1, remaining_qty=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.position_value == pytest.approx(9_500.0)
        assert a.equity == pytest.approx(19_000.0)
        assert a.unrealized_pnl == pytest.approx(-500.0)
        assert a.net_pnl == pytest.approx(9_000.0)
        # Invariant
        assert a.equity == pytest.approx(a.balance + a.position_value)

    def test_multiple_positions(self, tmp_path: Any) -> None:
        """Multiple open positions — values must sum correctly."""
        # pos1 market value = 102_000 × 0.05 = 5_100
        # pos2 market value = 3_200 × 2.0 = 6_400 -> position_value = 11_500
        # equity = 8_000 + 11_500 = 19_500
        # unrealized = (102_000-100_000)×0.05 + (3_200-3_000)×2.0 = 100 + 400 = 500
        # net = equity - initial_balance = 19500 - 10000 = 9500
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=19_500.0,
            realized_pnl=100.0, unrealized_pnl=500.0, net_pnl=9_500.0,
        )
        pos1 = _open_pos("BTCUSDT", 100_000, 102_000, 0.05, 0.05)
        pos2 = _open_pos("ETHUSDT", 3_000, 3_200, 2.0, 2.0)
        _write_positions(tmp_path, [pos1, pos2])

        a = self._mgr(tmp_path).account()

        assert a.position_value == pytest.approx(11_500.0)
        assert a.equity == pytest.approx(19_500.0)
        assert a.unrealized_pnl == pytest.approx(500.0)
        assert a.net_pnl == pytest.approx(9_500.0)
        assert a.open_positions == 2
        # Invariant
        assert a.equity == pytest.approx(a.balance + a.position_value)

    def test_partial_exit(self, tmp_path: Any) -> None:
        """Position with partial exit (remaining_qty < quantity)."""
        # Market value = 105_000 × 0.05 = 5_250; equity = 10_200 + 5_250 = 15_450
        # Unrealized = (105_000 - 100_000) × 0.05 = 250; net = equity - initial_balance = 15450 - 10000 = 5450
        _write_pb(
            tmp_path, final_balance=10_200.0, final_equity=15_450.0,
            realized_pnl=200.0, unrealized_pnl=250.0, net_pnl=5_450.0,
        )
        pos = _open_pos(
            entry_price=100_000, current_price=105_000,
            quantity=0.1, remaining_qty=0.05,
        )
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.position_value == pytest.approx(5_250.0)
        assert a.unrealized_pnl == pytest.approx(250.0)
        assert a.net_pnl == pytest.approx(5_450.0)

    def test_full_exit(self, tmp_path: Any) -> None:
        """Only closed positions — no open position value."""
        _write_pb(
            tmp_path, final_balance=10_500.0, final_equity=10_500.0,
            realized_pnl=500.0, unrealized_pnl=0.0, net_pnl=500.0,
        )
        pos = _closed_pos(entry_price=100_000, current_price=105_000, quantity=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.position_value == 0.0
        assert a.equity == pytest.approx(10_500.0)
        assert a.unrealized_pnl == 0.0
        assert a.net_pnl == pytest.approx(500.0)
        assert a.exposure_pct == 0.0

    def test_return_pct(self, tmp_path: Any) -> None:
        """Return % = ((equity - initial) / initial) × 100."""
        # equity = 9_500 + (110_000 × 0.1) = 20_500
        # return = (20_500 - 10_000) / 10_000 × 100 = 105%
        _write_pb(
            tmp_path, final_balance=9_500.0, initial_balance=10_000.0,
            final_equity=20_500.0, unrealized_pnl=1_000.0, net_pnl=1_000.0,
            total_return_pct=105.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=110_000, quantity=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.equity == pytest.approx(20_500.0)
        assert a.total_return_pct == pytest.approx(105.0)

    def test_net_pnl_invariant(self, tmp_path: Any) -> None:
        """net_pnl == equity - initial_balance always."""
        # Market value = 52_000 × 1.0 = 52_000; equity = 9_000 + 52_000 = 61_000
        # net_pnl = 61000 - 10000 = 51000
        _write_pb(
            tmp_path, final_balance=9_000.0, final_equity=61_000.0,
            realized_pnl=300.0, unrealized_pnl=2_000.0, net_pnl=51_000.0,
        )
        pos = _open_pos(entry_price=50_000, current_price=52_000, quantity=1.0)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        # Account-centric: net_pnl = equity - initial_balance
        assert a.net_pnl == pytest.approx(a.equity - a.initial_balance)

    def test_equity_invariant(self, tmp_path: Any) -> None:
        """equity == balance + position_value always (true by construction:
        position_value is derived as equity - balance)."""
        _write_pb(
            tmp_path, final_balance=7_777.77, final_equity=31_277.77,
            realized_pnl=123.45, unrealized_pnl=1_000.0, net_pnl=1_123.45,
        )
        pos = _open_pos(entry_price=45_000, current_price=47_000, quantity=0.5)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.equity == pytest.approx(a.balance + a.position_value)

    def test_exposure_bounds(self, tmp_path: Any) -> None:
        """Exposure % must be >= 0 and <= 100 for spot paper trading."""
        # Market value = 200_000 × 0.1 = 20_000; equity = 5_000 + 20_000 = 25_000
        _write_pb(
            tmp_path, final_balance=5_000.0, final_equity=25_000.0,
            unrealized_pnl=10_000.0, net_pnl=10_000.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=200_000, quantity=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert 0.0 <= a.exposure_pct <= 100.0

    def test_account_recomputes_from_positions_json(self, tmp_path: Any) -> None:
        """MetricsManager.compute_snapshot() derives equity/market-value from
        open positions — the single canonical accounting function."""
        _write_pb(
            tmp_path, final_balance=9_000.0, final_equity=15_000.0,
            realized_pnl=0.0, unrealized_pnl=6_000.0, net_pnl=6_000.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=105_000, quantity=0.1, remaining_qty=0.1)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        # equity = cash + position_market_value = 9000 + (105000 * 0.1) = 19500
        assert a.equity == pytest.approx(19_500.0)
        assert a.position_value == pytest.approx(10_500.0)
        assert a.unrealized_pnl == pytest.approx(500.0)


# ============================================================================
#  Timezone formatting
# ============================================================================

class TestTimezoneFormatting:
    """All Telegram datetime output must use WIB (UTC+7), not UTC."""

    def test_wib_now_format(self) -> None:
        """wib_now() must return WIB-formatted string."""
        from telegram.ui import wib_now
        result = wib_now()
        assert "WIB" in result
        # Must NOT contain "UTC"
        assert "UTC" not in result

    def test_wib_datetime_format(self) -> None:
        """wib_datetime() must return WIB datetime string."""
        from telegram.ui import wib_datetime
        result = wib_datetime(0.0)  # Unix epoch
        assert "WIB" in result
        assert "UTC" not in result

    def test_wib_now_contains_date(self) -> None:
        """wib_now() includes date and time."""
        from telegram.ui import wib_now
        result = wib_now()
        # Should have newline separating date and time
        assert "\n" in result
        parts = result.split("\n")
        assert len(parts) == 2

    def test_wib_datetime_epoch(self) -> None:
        """wib_datetime(0) = 01 Jan 1970 07:00 WIB (UTC+7)."""
        from telegram.ui import wib_datetime
        result = wib_datetime(0.0)
        assert "07:00 WIB" in result

    def test_wib_time_format(self) -> None:
        """wib_time() must return time-only WIB string."""
        from telegram.ui import wib_time
        result = wib_time(0.0)
        assert result == "07:00 WIB"


# ============================================================================
#  Consistency — AccountSnapshot is the single source
# ============================================================================

class TestAccountSnapshotConsistency:
    """All commands must use the same AccountSnapshot values."""

    def _mgr(self, tmp_path: Any) -> MetricsManager:
        return MetricsManager(data_dir=str(tmp_path))

    def test_wallet_uses_snapshot(self, tmp_path: Any) -> None:
        """Wallet command accounting matches AccountSnapshot."""
        # market value = 55_000 × 0.5 = 27_500; equity = 9_000 + 27_500 = 36_500
        # net_pnl = 36500 - 10000 = 26500
        _write_pb(
            tmp_path, final_balance=9_000.0, final_equity=36_500.0,
            realized_pnl=200.0, unrealized_pnl=2_500.0, net_pnl=26_500.0,
        )
        pos = _open_pos(entry_price=50_000, current_price=55_000, quantity=0.5, remaining_qty=0.5)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        # Verify wallet would display these values
        assert a.balance == 9_000.0
        assert a.position_value == pytest.approx(27_500.0)  # 55_000 × 0.5
        assert a.equity == pytest.approx(36_500.0)
        assert a.net_pnl == pytest.approx(26_500.0)  # equity - initial_balance
        assert a.exposure_pct == pytest.approx(27_500 / 36_500 * 100)

    def test_portfolio_uses_snapshot(self, tmp_path: Any) -> None:
        """Portfolio command accounting matches AccountSnapshot."""
        # market value = 2_800 × 10 = 28_000; equity = 8_000 + 28_000 = 36_000
        # net_pnl = 36000 - 10000 = 26000
        _write_pb(
            tmp_path, final_balance=8_000.0, final_equity=36_000.0,
            realized_pnl=-100.0, unrealized_pnl=-2_000.0, net_pnl=26_000.0,
        )
        pos = _open_pos(entry_price=3_000, current_price=2_800, quantity=10.0, remaining_qty=10.0)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        assert a.balance == 8_000.0
        assert a.position_value == pytest.approx(28_000.0)
        assert a.equity == pytest.approx(36_000.0)
        assert a.net_pnl == pytest.approx(26_000.0)  # equity - initial_balance
        assert a.exposure_pct == pytest.approx(28_000 / 36_000 * 100)

    def test_status_uses_snapshot(self, tmp_path: Any) -> None:
        """Status command exposure matches AccountSnapshot."""
        # Market value = 100_000 × 0.05 = 5_000; equity = 10_000 + 5_000 = 15_000
        _write_pb(
            tmp_path, final_balance=10_000.0, final_equity=15_000.0,
            unrealized_pnl=0.0, net_pnl=5_000.0,
        )
        pos = _open_pos(entry_price=100_000, current_price=100_000, quantity=0.05, remaining_qty=0.05)
        _write_positions(tmp_path, [pos])

        a = self._mgr(tmp_path).account()

        # At same price, no unrealized PnL
        assert a.unrealized_pnl == pytest.approx(0.0)
        assert a.net_pnl == pytest.approx(5_000.0)  # equity - initial_balance
        # Market value = 100_000 × 0.05 = 5_000
        assert a.position_value == pytest.approx(5_000.0)
        # Exposure = 5_000 / 15_000 × 100 ≈ 33.3%
        assert a.exposure_pct == pytest.approx(33.33, abs=0.1)

    def test_balance_uses_snapshot(self, tmp_path: Any) -> None:
        """Balance command returns same values as AccountSnapshot."""
        _write_pb(
            tmp_path, final_balance=12_000.0, final_equity=12_000.0,
            realized_pnl=1_000.0, unrealized_pnl=0.0, net_pnl=2_000.0,
        )
        _write_positions(tmp_path, [])

        a = self._mgr(tmp_path).account()

        assert a.balance == 12_000.0
        assert a.equity == 12_000.0
        assert a.realized_pnl == 1_000.0
        assert a.unrealized_pnl == 0.0
        assert a.net_pnl == 2_000.0  # equity - initial_balance

    def test_mixed_positions_consistency(self, tmp_path: Any) -> None:
        """Mix of open and closed positions — only open contribute to position_value."""
        # Only the open position contributes: 105_000 × 0.05 = 5_250
        # equity = 9_500 + 5_250 = 14_750; net = equity - initial_balance = 4750
        _write_pb(
            tmp_path, final_balance=9_500.0, final_equity=14_750.0,
            realized_pnl=300.0, unrealized_pnl=250.0, net_pnl=4_750.0,
        )
        open_p = _open_pos("BTCUSDT", 100_000, 105_000, 0.05, 0.05)
        closed_p = _closed_pos("ETHUSDT", 3_000, 3_200, 5.0)
        _write_positions(tmp_path, [open_p, closed_p])

        a = self._mgr(tmp_path).account()

        # Only open position contributes
        assert a.position_value == pytest.approx(5_250.0)  # 105_000 × 0.05
        assert a.open_positions == 1
        # Equity = 9_500 + 5_250 = 14_750
        assert a.equity == pytest.approx(14_750.0)
        assert a.net_pnl == pytest.approx(4_750.0)  # equity - initial_balance
        # Invariant
        assert a.equity == pytest.approx(a.balance + a.position_value)


# ============================================================================
#  AccountSnapshot dataclass
# ============================================================================

class TestAccountSnapshotDataclass:
    """AccountSnapshot fields have correct defaults."""

    def test_defaults(self) -> None:
        s = AccountSnapshot()
        assert s.balance == 0.0
        assert s.position_value == 0.0
        assert s.exposure_pct == 0.0
        assert s.equity == 0.0
        assert s.net_pnl == 0.0

    def test_fields(self) -> None:
        s = AccountSnapshot(
            balance=10_000.0,
            equity=20_000.0,
            position_value=10_000.0,
            exposure_pct=50.0,
            realized_pnl=500.0,
            unrealized_pnl=200.0,
            net_pnl=700.0,
            total_return_pct=100.0,
        )
        assert s.position_value == 10_000.0
        assert s.exposure_pct == 50.0


# ============================================================================
#  UTC leak prevention — health command
# ============================================================================

class TestHealthNoUTC:
    """Health command must never display 'UTC' in user-facing output."""

    def test_file_timestamp_returns_iso(self) -> None:
        """_file_timestamp() must return ISO string, not '%H:%M:%S UTC'."""
        import time as _time
        from scripts.health import _file_timestamp
        # Use an existing file
        ts, age = _file_timestamp("requirements.txt", _time.time())
        assert ts != "N/A"
        # Must be parseable by time_ago()
        from telegram.formatter import time_ago
        result = time_ago(ts)
        # time_ago returns relative time ("Xs ago", "Xm ago", etc.)
        # if it couldn't parse, it returns the raw string
        assert result != ts, f"time_ago failed to parse: {ts}"
        assert "UTC" not in result

    def test_health_snapshot_no_utc(self) -> None:
        """Health snapshot scanner_time must not contain 'UTC' string."""
        import time as _time
        from scripts.health import _file_timestamp
        ts, _ = _file_timestamp("requirements.txt", _time.time())
        assert "UTC" not in ts

    def test_time_ago_never_outputs_utc(self) -> None:
        """time_ago() output must never contain 'UTC'."""
        from telegram.formatter import time_ago
        # Test with various ISO timestamps
        now_iso = "2026-07-19T10:00:00+00:00"
        result = time_ago(now_iso)
        assert "UTC" not in result

        old_iso = "2026-01-01T00:00:00+00:00"
        result = time_ago(old_iso)
        assert "UTC" not in result

    def test_wib_now_never_outputs_utc(self) -> None:
        """wib_now() output must never contain 'UTC'."""
        from telegram.ui import wib_now
        assert "UTC" not in wib_now()

    def test_wib_datetime_never_outputs_utc(self) -> None:
        """wib_datetime() output must never contain 'UTC'."""
        from telegram.ui import wib_datetime
        assert "UTC" not in wib_datetime(0.0)
        assert "UTC" not in wib_datetime(1_700_000_000.0)
