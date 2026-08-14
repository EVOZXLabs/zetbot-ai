"""Regression tests: single source of truth for accounting / history /
summary / positions, legacy-quote protection, and reconciliation repair.

Architecture under test (see SPEC / audit):
  * OPEN POSITIONS = positions.json
  * CLOSED TRADES  = paper_trade_history.csv   (single source of truth)
  * ACCOUNTING     = paper_balance.json
  * SUMMARY / HISTORY / STATUS all derive from the SAME sources, so they
    can never disagree.

All file access is redirected to per-test temp dirs; the bot's live
``data/`` files are never touched.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any

import pytest

from scripts.metrics_manager import MetricsManager
from scripts.paper_state_lock import rebuild_trade_history_csv
from scripts.accounting_reconcile import reconcile


def _write(name: str, blob: Any) -> None:
    with open(f"data/{name}", "w") as f:
        json.dump(blob, f, indent=2)


def _seed_closed_trades(
    data_dir: str,
    trades: list[dict[str, Any]],
    account_quote: str = "IDR",
    open_positions: list[dict[str, Any]] | None = None,
    balance: dict[str, Any] | None = None,
) -> None:
    """Build a consistent PAPER state: paper_state.json (closed SELL orders
    + optional open positions) and a paper_balance.json shell."""
    os.makedirs(data_dir, exist_ok=True)

    orders = []
    positions: dict[str, Any] = {}
    for i, t in enumerate(trades):
        sym = t["symbol"]
        orders.append({
            "id": f"po-{i}", "symbol": sym, "side": "SELL",
            "type": "MARKET", "quantity": t.get("quantity", 1.0),
            "filled_quantity": t.get("quantity", 1.0),
            "entry_price": t.get("entry_price", 0.0),
            "fill_price": t.get("exit_price", 0.0),
            "slippage": 0.0, "entry_fee": 0.0, "exit_fee": 0.0,
            "exit_price": t.get("exit_price", 0.0),
            "total_cost": 0.0, "total_proceeds": 0.0,
            "net_pnl": t.get("net_pnl", 0.0),
            "net_pnl_pct": t.get("net_pnl_pct", 0.0),
            "status": "CLOSED",
            "created_at": t.get("closed_at", ""),
            "filled_at": t.get("closed_at", ""),
            "closed_at": t.get("closed_at", ""),
            "exit_reason": "manual",
        })
        positions[sym] = {
            "symbol": sym, "order_id": f"po-{i}",
            "quantity": t.get("quantity", 1.0),
            "remaining_qty": 0.0,
            "entry_price": t.get("entry_price", 0.0),
            "current_price": t.get("exit_price", 0.0),
            "unrealized_pnl": 0.0, "realized_pnl": t.get("net_pnl", 0.0),
            "total_pnl": t.get("net_pnl", 0.0),
            "cost_basis": t.get("entry_price", 0.0) * t.get("quantity", 1.0),
            "status": "CLOSED", "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
            "stop_loss": 0.0, "opened_at": t.get("closed_at", ""),
            "closure_notified": True,
        }
    for p in (open_positions or []):
        positions[p["symbol"]] = p

    _write("paper_state.json", {
        "version": 1, "balance": balance.get("final_balance", 300000.0)
        if balance else 300000.0,
        "initial_balance": 300000.0, "margin_used": 0.0,
        "orders": orders, "positions": positions, "equity_history": [],
    })
    _write("paper_balance.json", balance or {
        "initial_balance": 300000.0,
        "final_balance": 300000.0, "final_equity": 300000.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "net_pnl": 0.0,
        "total_return_pct": 0.0, "total_trades": 0, "winning_trades": 0,
        "losing_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
        "gross_profit": 0.0, "gross_loss": 0.0,
    })
    _write("positions.json", {
        "positions": list(positions.values()),
        "total_positions": len(positions),
        "active_count": sum(1 for p in positions.values()
                            if p.get("status") == "OPEN"),
        "closed_count": sum(1 for p in positions.values()
                            if p.get("status") != "OPEN"),
    })


# ---------------------------------------------------------------------------
#  1. Legacy BTC/USDT must NOT appear on an IDR account
# ---------------------------------------------------------------------------

class TestLegacyQuoteProtection:
    def test_legacy_usdt_trade_excluded_from_history(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [
            {"symbol": "BTC/USDT", "net_pnl": 40.0, "entry_price": 100,
             "exit_price": 140, "quantity": 1.0, "closed_at": "2026-08-11T08:00:00Z"},
            {"symbol": "XAUT/IDR", "net_pnl": -10.0, "entry_price": 77023101,
             "exit_price": 75777260, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
        ])
        rebuild_trade_history_csv("data")
        m = MetricsManager("data", account_quote="IDR")
        assert [t["symbol"] for t in m.trade_history()] == ["XAUT/IDR"]

    def test_legacy_usdt_excluded_from_open_positions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [], open_positions=[
            {"symbol": "BTC/USDT", "remaining_qty": 1.0, "entry_price": 100,
             "current_price": 110, "cost_basis": 100, "status": "OPEN"},
            {"symbol": "DODO/IDR", "remaining_qty": 38.0, "entry_price": 389,
             "current_price": 390, "cost_basis": 15000, "status": "OPEN"},
        ])
        m = MetricsManager("data", account_quote="IDR")
        assert [p["symbol"] for p in m.open_positions()] == ["DODO/IDR"]

    def test_legacy_allowed_when_account_is_usdt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "USDT")
        _seed_closed_trades("data", [
            {"symbol": "BTC/USDT", "net_pnl": 40.0, "entry_price": 100,
             "exit_price": 140, "quantity": 1.0, "closed_at": "2026-08-11T08:00:00Z"},
        ])
        rebuild_trade_history_csv("data")
        m = MetricsManager("data", account_quote="USDT")
        assert [t["symbol"] for t in m.trade_history()] == ["BTC/USDT"]


# ---------------------------------------------------------------------------
#  2. Closed trade appears in history, summary AND accounting
# ---------------------------------------------------------------------------

class TestClosedTradeConsistency:
    def test_closed_trade_in_history(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _seed_closed_trades("data", [
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
        ])
        rebuild_trade_history_csv("data")
        m = MetricsManager("data")
        assert len(m.trade_history()) == 1
        assert m.trade_history()[0]["symbol"] == "XAUT/IDR"

    def test_closed_trade_reflected_in_accounting(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
        ])
        rebuild_trade_history_csv("data")
        # reconcile() syncs paper_balance.json derived stats from the
        # canonical CSV, so accounting reflects the closed trade.
        reconcile(__import__("logging").getLogger("t"),
                  account_balance=300000.0)
        m = MetricsManager("data")
        a = m.account()
        assert a.total_trades == 1
        assert a.realized_pnl == pytest.approx(120.0, abs=0.01)
        assert a.win_rate == pytest.approx(100.0, abs=0.01)

    def test_summary_and_history_same_closed_count(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        # Timestamps are generated relative to "now" so the WIB-day boundary
        # check (trades_since_wib_midnight) is deterministic at any hour the
        # suite runs — a hardcoded timestamp at 08:xx UTC fails after 17:00
        # UTC when WIB has already rolled to the next day.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # Anchor to the most recent 00:00 WIB instead of ``now - 3h``: the
        # naive offset breaks between 17:00-20:00 UTC (WIB already on the
        # next calendar day, but now-3h still lands on the previous one),
        # which makes the "same closed count" assertion flaky by run hour.
        now_wib = now + timedelta(hours=7)
        wib_midnight = now_wib.replace(hour=0, minute=0, second=0, microsecond=0)
        base = wib_midnight.astimezone(timezone.utc) + timedelta(hours=1)
        trades = [
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012,
             "closed_at": base.isoformat()},
            {"symbol": "DODO/IDR", "net_pnl": -20.0, "entry_price": 389,
             "exit_price": 380, "quantity": 10,
             "closed_at": (base + timedelta(minutes=5)).isoformat()},
            {"symbol": "BICO/IDR", "net_pnl": 50.0, "entry_price": 891,
             "exit_price": 910, "quantity": 5,
             "closed_at": (base + timedelta(minutes=10)).isoformat()},
        ]
        _seed_closed_trades("data", trades)
        rebuild_trade_history_csv("data")
        reconcile(__import__("logging").getLogger("t"), account_balance=300000.0)
        m = MetricsManager("data")
        history = m.trade_history()
        summary = m.today_summary_wib()
        assert summary["total_trades"] == len(history) == 3
        # win rate identical between the two views
        assert summary["win_rate"] == pytest.approx(m.account().win_rate, abs=0.01)


# ---------------------------------------------------------------------------
#  3. Balance / status / accounting always identical
# ---------------------------------------------------------------------------

class TestBalanceStatusConsistency:
    def test_account_equity_invariant(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _seed_closed_trades("data", [
            {"symbol": "XAUT/IDR", "net_pnl": -10.0, "entry_price": 77023101,
             "exit_price": 75777260, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
        ], open_positions=[
            {"symbol": "DODO/IDR", "remaining_qty": 38.0, "entry_price": 389,
             "current_price": 390, "cost_basis": 15000, "status": "OPEN"},
        ], balance={
            "initial_balance": 300000.0, "final_balance": 284980.5,
            "final_equity": 284980.5, "realized_pnl": -10.0,
            "unrealized_pnl": 0.0, "net_pnl": -29.5, "total_return_pct": -0.01,
            "total_trades": 1, "winning_trades": 0, "losing_trades": 1,
            "win_rate": 0.0, "profit_factor": 0.0, "gross_profit": 0.0,
            "gross_loss": 10.0,
        })
        rebuild_trade_history_csv("data")
        m = MetricsManager("data")
        a = m.account()
        open_val = 390 * 38.0  # current_price * remaining_qty of DODO/IDR
        # equity == cash + open_position_value  (single-source invariant)
        assert a.equity == pytest.approx(a.balance + open_val, abs=0.5)
        # net_pnl == equity - initial_balance
        assert a.net_pnl == pytest.approx(a.equity - a.initial_balance, abs=0.5)
        # realized_pnl comes from closed trades (XAUT = -10)
        assert a.realized_pnl == pytest.approx(-10.0, abs=0.5)
        assert a.total_trades == 1


# ---------------------------------------------------------------------------
#  4. Reconciliation rebuilds drifted data
# ---------------------------------------------------------------------------

class TestReconciliationHardening:
    def test_reconcile_rebuilds_csv_and_stats(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        # paper_balance.json has STALE/WRONG derived stats; paper_state has
        # the real closed trades. Reconcile must rebuild the CSV and fix the
        # derived stats to match trade history.
        _seed_closed_trades("data", [
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
            {"symbol": "DODO/IDR", "net_pnl": -20.0, "entry_price": 389,
             "exit_price": 380, "quantity": 10, "closed_at": "2026-08-11T09:00:00Z"},
        ], balance={
            "initial_balance": 300000.0, "final_balance": 300000.0,
            "final_equity": 300000.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "net_pnl": 0.0, "total_return_pct": 0.0,
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate": 0.0, "profit_factor": 0.0, "gross_profit": 0.0,
            "gross_loss": 0.0,
        })
        # Corrupt the CSV so we prove reconcile rebuilds it.
        with open("data/paper_trade_history.csv", "w") as f:
            f.write("id,symbol,side\n")  # empty of real rows

        findings = reconcile(logger_obj=__import__("logging").getLogger("t"),
                             account_balance=300000.0)
        assert findings["trade_history_rebuilt"] == 2
        assert findings["repairs_applied"] > 0

        # after reconcile, paper_balance.json matches trade history
        with open("data/paper_balance.json") as f:
            pb = json.load(f)
        assert pb["total_trades"] == 2
        assert pb["winning_trades"] == 1
        assert pb["realized_pnl"] == pytest.approx(100.0, abs=0.01)
        # and MetricsManager reads the same
        m = MetricsManager("data", account_quote="IDR")
        assert m.account().total_trades == 2

    def test_reconcile_drops_legacy_trades(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [
            {"symbol": "BTC/USDT", "net_pnl": 40.0, "entry_price": 100,
             "exit_price": 140, "quantity": 1.0, "closed_at": "2026-08-11T08:00:00Z"},
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
        ])
        findings = reconcile(logger_obj=__import__("logging").getLogger("t"),
                             account_balance=300000.0)
        assert findings["legacy_trades_dropped"] == 1
        with open("data/paper_balance.json") as f:
            pb = json.load(f)
        # only the IDR trade counts
        assert pb["total_trades"] == 1
        assert pb["realized_pnl"] == pytest.approx(120.0, abs=0.01)


# ---------------------------------------------------------------------------
#  4b. Reconcile must drop legacy-quote POSITIONS, never re-open them
# ---------------------------------------------------------------------------

class TestReconcileLegacyPositions:
    """A position quoted in a different currency than the account (e.g.
    BTC/USDT on an IDR account) is a leftover from a previous exchange.
    Reconcile must purge it from positions.json AND paper_state.json and
    must never re-create it from a FILLED BUY order (three-writer drift)."""

    def _open_pos(self, symbol, **over):
        pos = {
            "symbol": symbol, "order_id": "po-x", "quantity": 1.0,
            "remaining_qty": 1.0, "entry_price": 100.0,
            "current_price": 110.0, "unrealized_pnl": 10.0,
            "realized_pnl": 0.0, "total_pnl": 10.0, "cost_basis": 100.0,
            "status": "OPEN", "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
            "stop_loss": 0.0, "opened_at": "2026-08-11T08:00:00Z",
            "closure_notified": False,
        }
        pos.update(over)
        return pos

    def test_reconcile_drops_legacy_positions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [], open_positions=[
            self._open_pos("BTC/USDT"),
            self._open_pos("DODO/IDR"),
        ])
        findings = reconcile(logger_obj=__import__("logging").getLogger("t"),
                             account_balance=300000.0)
        assert findings["legacy_positions_dropped"] == 1
        with open("data/positions.json") as f:
            pos_json = json.load(f)
        assert [p["symbol"] for p in pos_json["positions"]] == ["DODO/IDR"]
        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert list(state["positions"]) == ["DODO/IDR"]

    def test_reconcile_never_reopens_legacy_from_orders(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [])
        # A FILLED BUY with no matching SELL would normally look like
        # three-writer drift and re-create the position — but not for a
        # legacy quote.
        _write("paper_orders.json", {"orders": [{
            "id": "po-legacy-1", "symbol": "BTC/USDT", "side": "BUY",
            "status": "FILLED", "quantity": 1.0, "filled_quantity": 1.0,
            "fill_price": 100.0, "total_cost": 100.0,
            "filled_at": "2026-08-11T08:00:00Z",
        }]})
        findings = reconcile(logger_obj=__import__("logging").getLogger("t"),
                             account_balance=300000.0)
        assert findings["three_writer_drift"] is False
        assert findings["legacy_positions_dropped"] == 1
        with open("data/positions.json") as f:
            pos_json = json.load(f)
        assert pos_json["positions"] == []


# ---------------------------------------------------------------------------
#  4c. Paper engine restart must not restore legacy-quote positions
# ---------------------------------------------------------------------------

class TestEngineLoadStateLegacy:
    """PaperTradingEngine._load_state() restores positions from
    paper_state.json — it must skip symbols whose quote differs from the
    account currency, or a legacy USDT position would be managed as OPEN
    and inflate the engine's equity."""

    def _vp(self, symbol, status="OPEN"):
        return {
            "symbol": symbol, "order_id": "po-x", "quantity": 1.0,
            "remaining_qty": 1.0, "entry_price": 100.0,
            "current_price": 110.0, "unrealized_pnl": 10.0,
            "realized_pnl": 0.0, "total_pnl": 10.0, "cost_basis": 100.0,
            "status": status, "tp1_sold": False, "tp2_sold": False,
            "tp3_sold": False, "opened_at": "2026-08-11T08:00:00Z",
            "signal_time": "", "closure_notified": False,
            "tp1": 0.0, "tp2": 0.0, "tp3": 0.0, "stop_loss": 0.0,
            "position_size_usdt": 0.0,
        }

    def test_load_state_drops_legacy_positions(self, tmp_path, monkeypatch):
        import scripts.paper_trading_engine as pte

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        os.makedirs("data", exist_ok=True)
        state_path = str(tmp_path / "data" / "paper_state.json")
        _write("paper_state.json", {
            "version": 1, "balance": 300000.0, "initial_balance": 300000.0,
            "margin_used": 0.0, "orders": [],
            "positions": {
                "BTC/USDT": self._vp("BTC/USDT"),
                "XAUT/IDR": self._vp("XAUT/IDR"),
            },
            "equity_history": [],
        })
        monkeypatch.setattr(pte, "STATE_PATH", state_path)

        engine = pte.PaperTradingEngine()
        assert list(engine.positions) == ["XAUT/IDR"]


# ---------------------------------------------------------------------------
#  5. Mixed IDR/USDT data must not corrupt the report
# ---------------------------------------------------------------------------

class TestMixedQuoteIsolation:
    def test_mixed_quotes_reported_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("QUOTE_CURRENCY", "IDR")
        _seed_closed_trades("data", [
            {"symbol": "BTC/USDT", "net_pnl": 999.0, "entry_price": 100,
             "exit_price": 140, "quantity": 1.0, "closed_at": "2026-08-11T08:00:00Z"},
            {"symbol": "XAUT/IDR", "net_pnl": 100.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
            {"symbol": "DODO/IDR", "net_pnl": -30.0, "entry_price": 389,
             "exit_price": 380, "quantity": 10, "closed_at": "2026-08-11T09:00:00Z"},
        ])
        rebuild_trade_history_csv("data")
        reconcile(__import__("logging").getLogger("t"), account_balance=300000.0)
        m = MetricsManager("data", account_quote="IDR")
        assert m.account().total_trades == 2
        assert m.account().realized_pnl == pytest.approx(70.0, abs=0.01)
        assert all(t["symbol"].endswith("IDR") for t in m.trade_history())


# ---------------------------------------------------------------------------
#  6. Restart must not lose closed trades / change win rate / realized pnl
# ---------------------------------------------------------------------------

class TestRestartPersistence:
    def test_restart_keeps_closed_trades(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        trades = [
            {"symbol": "XAUT/IDR", "net_pnl": 120.0, "entry_price": 77023101,
             "exit_price": 78000000, "quantity": 0.0012, "closed_at": "2026-08-11T08:48:39Z"},
            {"symbol": "DODO/IDR", "net_pnl": -20.0, "entry_price": 389,
             "exit_price": 380, "quantity": 10, "closed_at": "2026-08-11T09:00:00Z"},
        ]
        _seed_closed_trades("data", trades)
        rebuild_trade_history_csv("data")
        before = MetricsManager("data")

        # Simulate a fresh process reading the persisted files (no in-memory
        # carry-over): rebuild CSV again, then re-read.
        rebuild_trade_history_csv("data")
        after = MetricsManager("data")

        assert len(after.trade_history()) == len(before.trade_history()) == 2
        assert after.account().win_rate == before.account().win_rate
        assert after.account().realized_pnl == before.account().realized_pnl
        assert after.account().total_trades == before.account().total_trades
