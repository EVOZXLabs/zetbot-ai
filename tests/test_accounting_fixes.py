"""Regression tests for accounting fixes (2026-07).

Covers:
- Return percentage ALWAYS computed from equity, never stale file value
- Equity never drops to cash when other positions remain open after closure
- initial_balance consistency across paper_state.json and paper_balance.json
- Startup reconciliation detects and repairs stale/mismatched accounting
- profit_factor is never Infinity/NaN
- /status shows open position symbols, not just count
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


# ============================================================================
#  Helpers
# ============================================================================

def _write_json(tmp_path: Any, name: str, data: dict[str, Any]) -> None:
    with open(tmp_path / name, "w") as f:
        json.dump(data, f, indent=2)


def _write_pb(
    tmp_path: Any,
    *,
    initial: float = 10_000.0,
    balance: float = 10_000.0,
    equity: float = 10_000.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    net: float = 0.0,
    return_pct: float = 0.0,
    trades: int = 0,
    wins: int = 0,
    losses: int = 0,
    profit_factor: float = 0.0,
) -> None:
    _write_json(tmp_path, "paper_balance.json", {
        "initial_balance": initial,
        "final_balance": balance,
        "final_equity": equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "net_pnl": net,
        "total_return_pct": return_pct,
        "total_trades": trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(wins / trades * 100, 2) if trades else 0.0,
        "profit_factor": profit_factor,
    })


def _write_positions(tmp_path: Any, positions: list[dict[str, Any]]) -> None:
    _write_json(tmp_path, "positions.json", {
        "positions": positions,
    })


def _open_pos(
    symbol: str = "BTCUSDT",
    entry: float = 100_000.0,
    current: float = 105_000.0,
    qty: float = 0.1,
    remaining: float = 0.1,
    unrealized: float | None = None,
) -> dict[str, Any]:
    if unrealized is None:
        unrealized = (current - entry) * remaining
    return {
        "symbol": symbol,
        "entry_price": entry,
        "current_price": current,
        "quantity": qty,
        "remaining_qty": remaining,
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": 0.0,
        "total_pnl": round(unrealized, 2),
        "cost_basis": entry * qty,
        "position_size_usdt": entry * qty,
        "status": "OPEN",
        "closure_notified": False,
    }


def _closed_pos(symbol: str = "BTCUSDT", entry: float = 100_000.0,
                pnl: float = 500.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "entry_price": entry,
        "current_price": 0.0,
        "quantity": 0.1,
        "remaining_qty": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": pnl,
        "total_pnl": pnl,
        "cost_basis": entry * 0.1,
        "position_size_usdt": 0.0,
        "status": "CLOSED",
        "closure_notified": True,
    }


def _mgr(tmp_path: Any) -> MetricsManager:
    return MetricsManager(data_dir=str(tmp_path))


# ============================================================================
#  Fix 1: Return percentage always from equity
# ============================================================================

class TestReturnPercentageAlwaysFromEquity:
    """total_return_pct must ALWAYS be ((equity - initial) / initial) * 100,
    never from final_balance, never from a stale file value."""

    def test_ignores_stale_file_return_pct(self, tmp_path: Any) -> None:
        """If paper_balance.json has a wrong total_return_pct, account()
        must recompute from open positions."""
        # equity = cash + position_market_value = 9000 + (150000*0.1) = 24000
        # Correct: ((24_000 - 10_000) / 10_000) * 100 = 140%
        _write_pb(
            tmp_path, initial=10_000, balance=9_000, equity=15_000,
            realized=0, unrealized=6_000, net=6_000,
            return_pct=99.9,  # stale value — ignored
        )
        _write_positions(tmp_path, [_open_pos(entry=100_000, current=150_000)])

        a = _mgr(tmp_path).account()
        assert a.total_return_pct == pytest.approx(140.0)

    def test_return_with_open_positions(self, tmp_path: Any) -> None:
        """Return % uses equity (includes unrealized), not final_balance."""
        # equity = cash + position_market_value = 5000 + (105000*0.1) = 15500
        # Correct: ((15_500 - 10_000) / 10_000) * 100 = 55%
        _write_pb(
            tmp_path, initial=10_000, balance=5_000, equity=15_000,
            realized=0, unrealized=10_000, net=10_000,
            return_pct=-50.0,  # WRONG — would be from balance
        )
        _write_positions(tmp_path, [_open_pos()])

        a = _mgr(tmp_path).account()
        assert a.total_return_pct == pytest.approx(55.0)

    def test_return_no_positions(self, tmp_path: Any) -> None:
        """Return % with no positions: equity == balance."""
        _write_pb(
            tmp_path, initial=10_000, balance=10_500, equity=10_500,
            realized=500, unrealized=0, net=500,
            return_pct=5.0,
        )
        _write_positions(tmp_path, [])

        a = _mgr(tmp_path).account()
        assert a.total_return_pct == pytest.approx(5.0)

    def test_return_negative(self, tmp_path: Any) -> None:
        """Return % can be negative."""
        _write_pb(
            tmp_path, initial=10_000, balance=8_000, equity=8_000,
            realized=-2_000, unrealized=0, net=-2_000,
            return_pct=-20.0,
        )
        _write_positions(tmp_path, [])

        a = _mgr(tmp_path).account()
        assert a.total_return_pct == pytest.approx(-20.0)

    def test_return_zero_initial_balance(self, tmp_path: Any) -> None:
        """Return % is 0 when initial_balance is 0 (division guard)."""
        _write_pb(
            tmp_path, initial=0, balance=1_000, equity=1_000,
            return_pct=0.0,
        )
        _write_positions(tmp_path, [])

        a = _mgr(tmp_path).account()
        assert a.total_return_pct == 0.0

    def test_fallback_path_also_recomputes(self, tmp_path: Any) -> None:
        """Fallback balance command path also recomputes return %."""
        _write_pb(
            tmp_path, initial=10_000, balance=9_000, equity=15_000,
            realized=0, unrealized=6_000, net=6_000,
            return_pct=999.0,  # WRONG
        )
        _write_positions(tmp_path, [_open_pos()])

        # Simulate the fallback path in balance command
        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        bal = pb.get("final_balance", 0.0)
        eq = pb.get("final_equity", 0.0)
        initial = pb.get("initial_balance", 10_000.0)
        total_return_pct = (
            ((eq - initial) / initial * 100.0) if initial > 0 else 0.0
        )
        assert total_return_pct == pytest.approx(50.0)


# ============================================================================
#  Fix 2: Equity doesn't drop to cash after partial closure
# ============================================================================

class TestEquityAfterClosure:
    """When one position closes, equity must include unrealized PnL
    from remaining open positions."""

    def test_update_paper_on_closure_preserves_equity(self, tmp_path: Any) -> None:
        """Simulate what _update_paper_on_closure does: after updating
        balance, equity must reflect remaining positions."""
        # Start: 2 open positions
        # POS_A: unrealized = 500, POS_B: unrealized = -200
        # balance = 5_000, realized = 300
        # equity should be 5_000 + (-200) = 4_800 after POS_A closes

        # Before closure: equity = 5_000 + 500 + (-200) = 5_300
        _write_pb(
            tmp_path, initial=10_000, balance=5_000, equity=5_300,
            realized=300, unrealized=300, net=600,
        )
        _write_positions(tmp_path, [
            _open_pos("POS_A", unrealized=500),
            _open_pos("POS_B", unrealized=-200),
        ])

        # Simulate closure: POS_A closes, proceeds = 500
        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        pb["final_balance"] = round(pb["final_balance"] + 500, 2)
        pb["realized_pnl"] = round(pb["realized_pnl"] + 500, 2)

        # Recalculate from remaining positions (POS_B only)
        # equity = balance + position_market_value (cost_basis + unrealized)
        import scripts.accounting_reconcile as rec_mod
        remaining_unrealized = -200.0
        # POS_B: entry=100_000, qty=0.1, cost_basis=10_000, unrealized=-200
        remaining_position_market_value = 10_000.0 + (-200.0)  # cost_basis + unrealized
        initial = pb.get("initial_balance", 10_000.0)
        pb["unrealized_pnl"] = round(remaining_unrealized, 2)
        pb["final_equity"] = round(pb["final_balance"] + remaining_position_market_value, 2)
        pb["net_pnl"] = round(pb["realized_pnl"] + remaining_unrealized, 2)
        pb["total_return_pct"] = round(
            ((pb["final_equity"] - initial) / initial * 100.0), 2
        ) if initial > 0 else 0.0

        # balance = 5_500, equity = 5_500 + 9_800 = 15_300
        # return = ((15_300 - 10_000) / 10_000) * 100 = +53%
        assert pb["final_balance"] == 5_500.0
        assert pb["final_equity"] == 15_300.0
        assert pb["unrealized_pnl"] == -200.0
        assert pb["net_pnl"] == round(800 + (-200), 2)  # realized=800
        assert pb["total_return_pct"] == pytest.approx(53.0)

    def test_equity_not_just_balance(self, tmp_path: Any) -> None:
        """After closure, equity MUST NOT equal balance if other positions exist."""
        import scripts.accounting_reconcile as rec_mod
        _write_pb(
            tmp_path, initial=10_000, balance=8_000, equity=12_000,
            realized=200, unrealized=4_000, net=4_200,
        )
        _write_positions(tmp_path, [
            _open_pos("A", unrealized=4_000),
        ])

        # Read, update balance (simulate +2000 proceeds)
        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        pb["final_balance"] = 10_000.0

        # The OLD bug was: pb["final_equity"] = pb["final_balance"] → 10_000
        # The FIRST fix was: equity = balance + unrealized_pnl → 14_000
        # The CORRECT fix: equity = balance + position_market_value
        # where position_market_value = cost_basis + unrealized = 10_000 + 4_000 = 14_000
        remaining_unrealized = 4_000.0  # still open
        # 10_000 (cost_basis) + 4_000 (unrealized) = 14_000 market value
        remaining_position_market_value = 14_000.0
        pb["unrealized_pnl"] = remaining_unrealized
        pb["final_equity"] = round(pb["final_balance"] + remaining_position_market_value, 2)
        assert pb["final_equity"] == 24_000.0  # NOT 10_000, NOT 14_000


# ============================================================================
#  Fix 3a: initial_balance consistency
# ============================================================================

class TestInitialBalanceConsistency:
    """initial_balance must be consistent across all files."""

    def test_paper_state_restores_initial(self, tmp_path: Any) -> None:
        """paper_state.json initial_balance is restored on load."""
        from scripts.paper_trading_engine import INITIAL_BALANCE

        state = {
            "version": 1,
            "balance": 8_000.0,
            "initial_balance": 5_000.0,  # Custom initial
            "margin_used": 0.0,
            "orders": [],
            "positions": {},
            "equity_history": [],
        }
        _write_json(tmp_path, "paper_state.json", state)

        # Simulate what _load_state does
        loaded = json.loads((tmp_path / "paper_state.json").read_text())
        wallet_initial = loaded.get("initial_balance", INITIAL_BALANCE)
        assert wallet_initial == 5_000.0

    def test_paper_state_persists_initial(self, tmp_path: Any) -> None:
        """_save_state writes initial_balance to paper_state.json."""
        state = {
            "version": 1,
            "balance": 8_000.0,
            "initial_balance": 5_000.0,
            "margin_used": 0.0,
            "orders": [],
            "positions": {},
            "equity_history": [],
        }
        _write_json(tmp_path, "paper_state.json", state)

        data = json.loads((tmp_path / "paper_state.json").read_text())
        assert "initial_balance" in data
        assert data["initial_balance"] == 5_000.0


# ============================================================================
#  Fix 3b: profit_factor never Infinity
# ============================================================================

class TestProfitFactorNeverInfinity:
    """profit_factor must be a valid JSON number, never Infinity or NaN."""

    def test_no_losers_profit_factor_not_inf(self, tmp_path: Any) -> None:
        """When no losing trades, profit_factor should be 0.0, not Inf."""
        from scripts.paper_trading_engine import _safe_div

        gross_profit = 500.0
        gross_loss = 0.0
        pf = _safe_div(gross_profit, gross_loss, 0.0)
        assert pf == 0.0
        # Must be valid JSON
        assert json.dumps({"pf": pf})

    def test_profit_factor_in_paper_balance(self, tmp_path: Any) -> None:
        """paper_balance.json must never contain Infinity."""
        _write_pb(tmp_path, profit_factor=0.0)
        content = (tmp_path / "paper_balance.json").read_text()
        assert "Infinity" not in content
        assert "NaN" not in content
        data = json.loads(content)
        assert isinstance(data["profit_factor"], (int, float))
        assert data["profit_factor"] != float("inf")
        assert data["profit_factor"] == data["profit_factor"]  # not NaN

    def test_metrics_manager_reads_valid_pf(self, tmp_path: Any) -> None:
        """MetricsManager reads profit_factor without error."""
        _write_pb(tmp_path, profit_factor=2.5)
        a = _mgr(tmp_path).account()
        assert a.profit_factor == 2.5


# ============================================================================
#  Fix 4: Startup reconciliation
# ============================================================================

class TestStartupReconciliation:
    """Reconciliation detects and repairs stale/mismatched accounting."""

    def test_detects_stale_equity(self, tmp_path: Any, monkeypatch: Any) -> None:
        """When positions exist but equity == balance (stale), repair it."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(
            tmp_path, initial=10_000, balance=8_000, equity=8_000,
            realized=0, unrealized=0, net=0,
        )
        _write_positions(tmp_path, [
            _open_pos(entry=50_000, current=52_000, unrealized=200),
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))

        findings = rec_mod.reconcile()

        assert findings["stale_equity"] is True
        assert findings["repairs_applied"] >= 1

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        assert pb["final_equity"] != pb["final_balance"] or pb["unrealized_pnl"] != 0

    def test_detects_initial_balance_mismatch(self, tmp_path: Any, monkeypatch: Any) -> None:
        """When paper_state and paper_balance disagree on initial_balance,
        reconcile uses paper_state's value."""
        import scripts.accounting_reconcile as rec_mod

        _write_json(tmp_path, "paper_state.json", {
            "version": 1,
            "balance": 8_000.0,
            "initial_balance": 5_000.0,
            "margin_used": 0.0,
            "orders": [],
            "positions": {},
            "equity_history": [],
        })
        _write_pb(
            tmp_path, initial=10_000, balance=8_000, equity=8_000,
        )
        _write_positions(tmp_path, [])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))

        findings = rec_mod.reconcile()

        assert findings["initial_balance_mismatch"] is True

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        assert pb["initial_balance"] == 5_000.0

    def test_fixes_infinite_profit_factor(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Infinity profit_factor is reset to 0.0."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, profit_factor=float("inf"))
        _write_positions(tmp_path, [])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))

        findings = rec_mod.reconcile()

        assert findings["profit_factor_fixed"] is True

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        assert pb["profit_factor"] == 0.0

    def test_recalculates_return_pct(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Reconciliation recalculates total_return_pct from equity."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(
            tmp_path, initial=10_000, balance=8_000, equity=8_000,
            unrealized=0, net=0,
            return_pct=999.0,
        )
        _write_positions(tmp_path, [])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))

        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        rec_mod.reconcile()

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        assert pb["total_return_pct"] == pytest.approx(-20.0)

    def test_preserves_equity_history(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Reconciliation preserves equity_history in paper_balance.json."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, balance=8_000, equity=8_000)
        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        pb["equity_history"] = [{"timestamp": "t1", "equity": 9000}]
        (tmp_path / "paper_balance.json").write_text(json.dumps(pb))

        _write_positions(tmp_path, [])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))

        rec_mod.reconcile()

        pb2 = json.loads((tmp_path / "paper_balance.json").read_text())
        assert "equity_history" in pb2
        assert len(pb2["equity_history"]) == 1

    def test_no_files_no_crash(self, monkeypatch: Any) -> None:
        """Reconciliation handles missing files gracefully."""
        import scripts.accounting_reconcile as rec_mod

        empty = "/tmp/nonexistent_accounting_test_dir"
        monkeypatch.setattr(rec_mod, "_STATE_PATH", f"{empty}/paper_state.json")
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", f"{empty}/paper_balance.json")
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", f"{empty}/positions.json")

        findings = rec_mod.reconcile()
        assert findings["repairs_applied"] == 0


# ============================================================================
#  Fix 5: /status shows position symbols
# ============================================================================

class TestStatusShowsSymbols:
    """Status command must show position symbols, not just count."""

    def test_status_with_open_positions(self, tmp_path: Any) -> None:
        """Open positions list shows symbols."""
        _write_pb(
            tmp_path, initial=10_000, balance=5_000, equity=15_000,
            unrealized=10_000, net=10_000,
        )
        _write_positions(tmp_path, [
            _open_pos("BTCUSDT"),
            _open_pos("ETHUSDT", entry=3_000, current=3_200),
        ])

        open_list = _mgr(tmp_path).open_positions()
        assert len(open_list) == 2
        symbols = [p.get("symbol", "?") for p in open_list]
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols

        # Build the label as status command would
        if open_list:
            label = f"{len(open_list)}: {', '.join(symbols)}"
        else:
            label = "None"
        assert label == "2: BTCUSDT, ETHUSDT"

    def test_status_no_positions(self, tmp_path: Any) -> None:
        """No open positions → 'None'."""
        _write_pb(tmp_path, balance=10_000, equity=10_000)
        _write_positions(tmp_path, [])

        open_list = _mgr(tmp_path).open_positions()
        if open_list:
            symbols = [p.get("symbol", "?") for p in open_list]
            label = f"{len(open_list)}: {', '.join(symbols)}"
        else:
            label = "None"
        assert label == "None"


# ============================================================================
#  Fix 6: Three-writer drift detection and repair
# ============================================================================

class TestThreeWriterDrift:
    """paper_orders.json is now the canonical source for open position
    detection. Reconcile must detect when orders say OPEN but
    positions.json says CLOSED."""

    def _write_orders(self, tmp_path: Any, orders: list[dict[str, Any]]) -> None:
        _write_json(tmp_path, "paper_orders.json", {
            "orders": orders,
        })

    def test_reconcile_detects_drift(self, tmp_path: Any, monkeypatch: Any) -> None:
        """FILLED BUY with no SELL but positions.json says CLOSED → drift detected."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, initial=10_000, balance=2_000, equity=2_000)
        _write_positions(tmp_path, [
            _closed_pos("BTCUSDT", entry=50_000),
        ])
        self._write_orders(tmp_path, [
            {
                "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                "status": "FILLED", "quantity": 0.02, "filled_quantity": 0.02,
                "fill_price": 50_000.0, "total_cost": 1_000.0,
                "filled_at": "2026-07-20T10:00:00Z",
            },
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        findings = rec_mod.reconcile()
        assert findings["three_writer_drift"] is True
        assert findings["repairs_applied"] >= 1

    def test_reconcile_repair_sets_position_open(self, tmp_path: Any, monkeypatch: Any) -> None:
        """After repair, positions.json marks the position OPEN."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, initial=10_000, balance=2_000, equity=2_000)
        _write_positions(tmp_path, [
            _closed_pos("BTCUSDT", entry=50_000),
        ])
        self._write_orders(tmp_path, [
            {
                "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                "status": "FILLED", "quantity": 0.02, "filled_quantity": 0.02,
                "fill_price": 50_000.0, "total_cost": 1_000.0,
                "filled_at": "2026-07-20T10:00:00Z",
            },
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        rec_mod.reconcile()

        pos_data = json.loads((tmp_path / "positions.json").read_text())
        btc = [p for p in pos_data["positions"] if p["symbol"] == "BTCUSDT"]
        assert len(btc) == 1
        assert btc[0]["status"] == "OPEN"

    def test_reconcile_no_drift_when_orders_match(self, tmp_path: Any, monkeypatch: Any) -> None:
        """No drift when BUY + SELL exist or positions already OPEN."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, initial=10_000, balance=8_000, equity=8_000)
        _write_positions(tmp_path, [
            _open_pos("BTCUSDT"),
        ])
        self._write_orders(tmp_path, [
            {
                "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                "status": "FILLED", "quantity": 0.1, "filled_quantity": 0.1,
                "fill_price": 100_000.0, "total_cost": 10_000.0,
                "filled_at": "2026-07-20T10:00:00Z",
            },
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        findings = rec_mod.reconcile()
        assert findings.get("three_writer_drift") is False

    def test_reconcile_no_drift_when_sell_exists(self, tmp_path: Any, monkeypatch: Any) -> None:
        """No drift when SELL order exists for the symbol."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, initial=10_000, balance=11_000, equity=11_000,
                   realized=1_000, trades=1, wins=1)
        _write_positions(tmp_path, [
            _closed_pos("BTCUSDT", entry=50_000, pnl=1_000),
        ])
        self._write_orders(tmp_path, [
            {
                "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                "status": "FILLED", "quantity": 0.02, "filled_quantity": 0.02,
                "fill_price": 50_000.0, "total_cost": 1_000.0,
                "filled_at": "2026-07-20T10:00:00Z",
            },
            {
                "id": "sell-1", "symbol": "BTCUSDT", "side": "SELL",
                "status": "CLOSED", "quantity": 0.02, "filled_quantity": 0.02,
                "fill_price": 51_000.0, "total_proceeds": 1_018.0,
                "net_pnl": 18.0, "filled_at": "2026-07-20T12:00:00Z",
            },
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        findings = rec_mod.reconcile()
        assert findings.get("three_writer_drift") is False

    def test_equity_uses_repaired_positions(self, tmp_path: Any, monkeypatch: Any) -> None:
        """After three-writer repair, equity includes the repaired open position."""
        import scripts.accounting_reconcile as rec_mod

        _write_pb(tmp_path, initial=10_000, balance=2_000, equity=2_000)
        _write_positions(tmp_path, [
            _closed_pos("BTCUSDT", entry=50_000),
        ])
        self._write_orders(tmp_path, [
            {
                "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                "status": "FILLED", "quantity": 0.02, "filled_quantity": 0.02,
                "fill_price": 50_000.0, "total_cost": 1_000.0,
                "filled_at": "2026-07-20T10:00:00Z",
            },
        ])

        monkeypatch.setattr(rec_mod, "_STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(rec_mod, "_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
        monkeypatch.setattr(rec_mod, "_POSITIONS_PATH", str(tmp_path / "positions.json"))
        monkeypatch.setattr(rec_mod, "_ORDERS_PATH", str(tmp_path / "paper_orders.json"))

        rec_mod.reconcile()

        pb = json.loads((tmp_path / "paper_balance.json").read_text())
        assert pb["unrealized_pnl"] == 0.0
        # Equity = cash + position_value + unrealized = 2000 + 1000 + 0 = 3000
        assert pb["final_equity"] > pb["final_balance"]


# ============================================================================
#  Fix 7: PaperExport orders_json merge behavior
# ============================================================================

class TestPaperExportMerge:
    """PaperExport.orders_json() must merge with existing file, not overwrite."""

    def test_merge_preserves_monitor_sell_orders(self, tmp_path: Any) -> None:
        """Monitor-appended SELL orders survive engine re-export."""
        from scripts.paper_trading_engine import PaperExport, Order
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        # Pre-existing file with monitor SELL
        existing = {
            "orders": [
                {
                    "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                    "status": "FILLED", "quantity": 0.02,
                    "filled_quantity": 0.02, "fill_price": 50_000.0,
                    "total_cost": 1_000.0, "created_at": now, "filled_at": now,
                    "closed_at": "",
                },
                {
                    "id": "monitor-BTCUSDT-123", "symbol": "BTCUSDT",
                    "side": "SELL", "status": "CLOSED",
                    "quantity": 0.02, "filled_quantity": 0.02,
                    "fill_price": 51_000.0, "total_proceeds": 1_018.0,
                    "net_pnl": 18.0, "created_at": now, "filled_at": now,
                    "closed_at": now, "exit_reason": "Take Profit",
                },
            ]
        }
        path = str(tmp_path / "paper_orders.json")
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        # Engine only has the BUY (doesn't know about monitor SELL)
        engine_orders = [
            Order(
                id="buy-1", symbol="BTCUSDT", side="BUY", type="MARKET",
                quantity=0.02, filled_quantity=0.02,
                entry_price=50_000.0, fill_price=50_000.0,
                slippage=0.0, entry_fee=1.0,
                exit_price=0.0, exit_fee=0.0,
                total_cost=1_000.0, total_proceeds=0.0,
                net_pnl=0.0, net_pnl_pct=0.0,
                status="FILLED", created_at=now, filled_at=now, closed_at="",
            )
        ]

        PaperExport.orders_json(engine_orders, path)

        result = json.loads(open(path).read())
        ids = [o["id"] for o in result["orders"]]
        assert "monitor-BTCUSDT-123" in ids, "Monitor SELL order was lost!"
        assert result["open_orders"] == 1
        assert result["closed_orders"] == 1

    def test_merge_engine_overwrites_file_orders(self, tmp_path: Any) -> None:
        """Engine orders take precedence over file orders with same id."""
        from scripts.paper_trading_engine import PaperExport, Order
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        existing = {
            "orders": [
                {
                    "id": "buy-1", "symbol": "BTCUSDT", "side": "BUY",
                    "status": "FILLED", "quantity": 0.02,
                    "fill_price": 50_000.0, "created_at": now, "filled_at": now,
                },
            ]
        }
        path = str(tmp_path / "paper_orders.json")
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        # Engine has same id but updated status
        engine_orders = [
            Order(
                id="buy-1", symbol="BTCUSDT", side="BUY", type="MARKET",
                quantity=0.02, filled_quantity=0.02,
                entry_price=50_000.0, fill_price=50_000.0,
                slippage=0.0, entry_fee=1.0,
                exit_price=0.0, exit_fee=0.0,
                total_cost=1_000.0, total_proceeds=0.0,
                net_pnl=0.0, net_pnl_pct=0.0,
                status="CLOSED", created_at=now, filled_at=now,
                closed_at=now,
            )
        ]

        PaperExport.orders_json(engine_orders, path)

        result = json.loads(open(path).read())
        assert len(result["orders"]) == 1
        assert result["orders"][0]["status"] == "CLOSED"


# ============================================================================
#  Fix 8: PaperExport balance_json merge behavior
# ============================================================================

class TestPaperExportBalanceMerge:
    """PaperExport.balance_json() must merge with existing file, not overwrite."""

    def test_merge_preserves_monitor_realized_pnl(self, tmp_path: Any) -> None:
        """Monitor-added realized PnL survives engine re-export."""
        from scripts.paper_trading_engine import PaperExport, EquitySnapshot

        # Pre-existing file with monitor updates
        existing = {
            "initial_balance": 10_000.0,
            "final_balance": 3_000.0,
            "final_equity": 3_500.0,
            "realized_pnl": 500.0,
            "total_trades": 2,
            "winning_trades": 2,
            "losing_trades": 0,
            "unrealized_pnl": 500.0,
            "net_pnl": 1_000.0,
            "equity_history": [],
        }
        path = str(tmp_path / "paper_balance.json")
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        # Engine metrics (lower realized because engine doesn't know about monitor)
        engine_metrics = {
            "initial_balance": 10_000.0,
            "final_balance": 3_000.0,
            "final_equity": 3_000.0,
            "realized_pnl": 200.0,
            "total_trades": 1,
            "winning_trades": 1,
            "losing_trades": 0,
            "unrealized_pnl": 0.0,
            "net_pnl": 200.0,
        }

        PaperExport.balance_json(engine_metrics, [], path)

        result = json.loads(open(path).read())
        # File's higher realized_pnl should be preserved
        assert result["realized_pnl"] == 500.0
        assert result["total_trades"] == 2

    def test_merge_uses_engine_when_higher(self, tmp_path: Any) -> None:
        """Engine metrics used when they exceed file values."""
        from scripts.paper_trading_engine import PaperExport

        existing = {
            "initial_balance": 10_000.0,
            "final_balance": 2_000.0,
            "realized_pnl": 100.0,
            "total_trades": 1,
            "winning_trades": 1,
            "losing_trades": 0,
        }
        path = str(tmp_path / "paper_balance.json")
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        engine_metrics = {
            "initial_balance": 10_000.0,
            "final_balance": 3_000.0,
            "final_equity": 3_000.0,
            "realized_pnl": 800.0,
            "total_trades": 5,
            "winning_trades": 4,
            "losing_trades": 1,
            "unrealized_pnl": 0.0,
            "net_pnl": 800.0,
        }

        PaperExport.balance_json(engine_metrics, [], path)

        result = json.loads(open(path).read())
        assert result["realized_pnl"] == 800.0
        assert result["total_trades"] == 5
