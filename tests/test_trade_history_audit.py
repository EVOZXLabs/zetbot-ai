"""
Regression tests for the position-lifecycle / trade-history audit.

Covers the HOME/IDR inconsistency found in production:

* paper_trade_history.csv stamped ``created_at == filled_at == closed_at``
  (all = SELL time) while Telegram showed the real holding duration
  (``opened_at`` -> close) — the SELL-order writers were stamping the sell
  moment as the trade's *created* time.
* ``exit_reason`` was dropped from the trade-history schema (CSV had no
  column; providers persisted a generic ``market_sell``).
* No historical evidence existed for the indicators/SL/TP at entry:
  ``decision_results.json`` / ``scanner_results.json`` are overwritten
  every pipeline run.

Assertions:
  * opened_at is preserved as the entry moment (provider sell, manual
    sell, engine reconcile).
  * closed_at is the sell fill time and never precedes opened_at.
  * holding duration derives from opened_at -> closed_at in the CSV and
    in MetricsManager / /history / /summary consumers.
  * exit_reason is persisted in the ledger and in the CSV schema.
  * entry indicator snapshots are write-once and survive later pipeline
    runs unchanged.
"""

import csv
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

import scripts.paper_state_lock as psl
from scripts.execution_provider import (
    PaperExecutionProvider,
    OrderRequest,
)
from scripts.paper_state_lock import (
    rebuild_trade_history_csv,
    save_entry_snapshot,
    load_entry_snapshots,
)
from scripts.metrics_manager import MetricsManager
from telegram.formatter import order_hold_seconds


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def paper_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect ALL paper-state paths (provider + snapshot store) to tmp."""
    import scripts.execution_provider as ep
    monkeypatch.setattr(ep, "PAPER_STATE_PATH", str(tmp_path / "paper_state.json"))
    monkeypatch.setattr(ep, "PAPER_BALANCE_PATH", str(tmp_path / "paper_balance.json"))
    monkeypatch.setattr(psl, "ENTRY_SNAPSHOTS_PATH", str(tmp_path / "entry_snapshots.json"))
    return tmp_path


def _write_context(
    data_dir: Any,
    symbol: str = "BTC/USDT",
    decision_overall: float = 84.8,
    ema50: float = 48000.0,
) -> None:
    """Simulate the pipeline-run context files (decision + scanner)."""
    with open(os.path.join(str(data_dir), "decision_results.json"), "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "decisions": [{
                "symbol": symbol,
                "probability": 84.8,
                "recommendation": "STRONG BUY",
                "trend_score": 90.0,
                "momentum_score": 80.0,
                "overall_score": decision_overall,
            }],
        }, f)
    with open(os.path.join(str(data_dir), "scanner_results.json"), "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "pairs": [{
                "symbol": symbol,
                "price": 50000.0,
                "ema50": ema50,
                "ema100": 47000.0,
                "ema200": 45000.0,
                "rsi14": 61.5,
                "adx14": 30.0,
                "atr_pct": 2.5,
                "trend_alignment": "BULLISH",
            }],
        }, f)


def _buy_request(symbol: str = "BTC/USDT", price: float = 50000.0,
                 qty: float = 0.02) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side="BUY",
        type="MARKET",
        amount=qty,
        price=price,
        stop_loss=49000.0,
        take_profit=51000.0,
        metadata={
            "tp1": 51000.0,
            "tp2": 52000.0,
            "tp3": 53000.0,
            "stop_loss": 49000.0,
            "position_size_usdt": 1000.0,
            "signal_time": "2026-08-12T14:47:47.560543+00:00",
        },
    )


def _sell_request(symbol: str = "BTC/USDT", price: float = 48000.0,
                  qty: float = 0.02, exit_level: str = "sl") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side="SELL",
        type="MARKET",
        amount=qty,
        price=price,
        metadata={"source": "execution_pipeline", "exit_level": exit_level},
    )


# ---------------------------------------------------------------------------
#  opened_at / closed_at correctness (provider path)
# ---------------------------------------------------------------------------


class TestProviderSellTimestamps:
    """PaperExecutionProvider must preserve opened_at as the entry moment."""

    def test_sell_order_keeps_position_opened_at(self, paper_dir: Any):
        provider = PaperExecutionProvider()
        _write_context(paper_dir)
        provider.execute_buy(_buy_request())
        opened_at = provider.positions["BTC/USDT"].opened_at
        assert opened_at, "position must carry opened_at"

        provider.execute_sell(_sell_request())

        import json as _json
        with open(ep_state(paper_dir)) as f:
            state = _json.load(f)
        sell = state["orders"][-1]
        assert sell["side"] == "SELL"
        assert sell["status"] == "CLOSED"
        # entry moment preserved, close = sell time
        assert sell["created_at"] == opened_at
        assert sell["filled_at"] == opened_at
        assert sell["closed_at"] >= opened_at
        assert sell["closed_at"] != opened_at

    def test_closed_at_never_precedes_opened_at(self, paper_dir: Any):
        provider = PaperExecutionProvider()
        _write_context(paper_dir)
        provider.execute_buy(_buy_request())
        opened_at = provider.positions["BTC/USDT"].opened_at

        provider.execute_sell(_sell_request())
        import json as _json
        with open(ep_state(paper_dir)) as f:
            state = _json.load(f)
        sell = state["orders"][-1]
        assert sell["closed_at"] >= opened_at

    def test_exit_reason_mapped_from_exit_level(self, paper_dir: Any):
        provider = PaperExecutionProvider()
        _write_context(paper_dir)
        provider.execute_buy(_buy_request())

        provider.execute_sell(_sell_request(exit_level="sl"))
        import json as _json
        with open(ep_state(paper_dir)) as f:
            state = _json.load(f)
        assert state["orders"][-1]["exit_reason"] == "Stop Loss"

        provider.execute_buy(_buy_request(symbol="ETH/USDT", price=3000.0, qty=0.3))
        provider.execute_sell(_sell_request(symbol="ETH/USDT", price=3100.0,
                                            qty=0.3, exit_level="tp1_hit"))
        with open(ep_state(paper_dir)) as f:
            state = _json.load(f)
        tp_order = [o for o in state["orders"] if o["symbol"] == "ETH/USDT"][-1]
        assert tp_order["exit_reason"] == "Take Profit"


def ep_state(paper_dir: Any) -> str:
    return str(paper_dir / "paper_state.json")


# ---------------------------------------------------------------------------
#  opened_at preservation (engine reconcile + manual sell)
# ---------------------------------------------------------------------------


class TestEngineAndManualSellTimestamps:
    """Engine reconcile and manual-sell paths keep entry timestamps too."""

    def test_engine_reconcile_sell_uses_opened_at(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ):
        import scripts.paper_trading_engine as pte
        monkeypatch.setattr(pte, "STATE_PATH", str(tmp_path / "paper_state.json"))
        monkeypatch.setattr(
            psl, "ENTRY_SNAPSHOTS_PATH", str(tmp_path / "entry_snapshots.json"),
        )
        engine = pte.PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0,
        }
        engine._execute_plan(plan, None)
        opened_at = engine.positions["BTC/USDT"].opened_at

        engine._reconcile({"symbol": "BTC/USDT"}, {
            "symbol": "BTC/USDT", "current_price": 48000.0,
            "status": "STOPPED",
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
            "tp1": 50500.0, "tp2": 52000.0, "tp3": 54000.0,
            "stop_loss": 49000.0, "current_stop": 49000.0,
        })
        closed = [o for o in engine.orders if o.status == "CLOSED"]
        assert len(closed) == 1
        assert closed[0].side == "SELL"
        assert closed[0].created_at == opened_at
        assert closed[0].filled_at == opened_at
        assert closed[0].closed_at >= opened_at
        assert closed[0].exit_reason == "Stop Loss"

    def test_manual_sell_preserves_opened_at(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ):
        from scripts.order_manager import _close_paper_position_on_sell
        opened_at = "2026-08-12T14:47:47.816460+00:00"
        state = {
            "version": 1,
            "balance": 90000.0,
            "positions": {
                "BTC/USDT": {
                    "symbol": "BTC/USDT", "order_id": "po_buy1",
                    "quantity": 1.0, "remaining_qty": 1.0,
                    "entry_price": 100.0, "current_price": 100.0,
                    "unrealized_pnl": 0.0, "realized_pnl": 0.0,
                    "total_pnl": 0.0, "cost_basis": 100.0,
                    "status": "OPEN", "opened_at": opened_at,
                },
            },
        }
        state_path = tmp_path / "data" / "paper_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(state, f)
        monkeypatch.chdir(tmp_path)

        _close_paper_position_on_sell("BTC/USDT", 1.0, 95.0, 94.0)

        with open(state_path) as f:
            state = json.load(f)
        sell = [o for o in state["orders"] if o["side"] == "SELL"][-1]
        assert sell["created_at"] == opened_at
        assert sell["filled_at"] == opened_at
        assert sell["closed_at"] != opened_at
        assert sell["exit_reason"] == "manual"


# ---------------------------------------------------------------------------
#  Trade-history CSV schema + holding duration
# ---------------------------------------------------------------------------


class TestTradeHistoryCsv:
    """CSV must carry exit_reason and entry timestamps -> real holding."""

    def test_csv_schema_includes_exit_reason(self, paper_dir: Any):
        provider = PaperExecutionProvider()
        _write_context(paper_dir)
        provider.execute_buy(_buy_request())
        provider.execute_sell(_sell_request())

        rebuild_trade_history_csv(str(paper_dir))
        with open(os.path.join(str(paper_dir), "paper_trade_history.csv"),
                  newline="") as f:
            reader = csv.DictReader(f)
            assert "exit_reason" in reader.fieldnames
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["exit_reason"] == "Stop Loss"
        opened_at = provider.positions["BTC/USDT"].opened_at
        assert rows[0]["created_at"] == opened_at
        assert rows[0]["filled_at"] == opened_at
        assert rows[0]["closed_at"] > opened_at

    def test_csv_legacy_row_self_heals_timestamps(self, paper_dir: Any):
        """Pre-fix rows (created_at == closed_at) are repaired on rebuild."""
        opened_at = "2026-08-12T14:47:47.816460+00:00"
        state = {
            "orders": [{
                "id": "po_legacy", "symbol": "HOME/IDR", "side": "SELL",
                "quantity": 535.7143, "entry_price": 168.0504,
                "fill_price": 160.9517, "exit_price": 160.9517,
                "net_pnl": -3979.13, "net_pnl_pct": -4.42,
                "status": "CLOSED",
                "created_at": "2026-08-12T18:41:36.451591+00:00",
                "filled_at": "2026-08-12T18:41:36.451650+00:00",
                "closed_at": "2026-08-12T18:41:36.451661+00:00",
                "exit_reason": "Stop Loss",
            }],
            "positions": {
                "HOME/IDR": {
                    "symbol": "HOME/IDR", "status": "CLOSED",
                    "opened_at": opened_at,
                },
            },
        }
        with open(os.path.join(str(paper_dir), "paper_state.json"), "w") as f:
            json.dump(state, f)
        with open(os.path.join(str(paper_dir), "paper_orders.json"), "w") as f:
            json.dump({"orders": [dict(state["orders"][0])]}, f)

        rebuild_trade_history_csv(str(paper_dir))
        with open(os.path.join(str(paper_dir), "paper_trade_history.csv"),
                  newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["created_at"] == opened_at
        assert rows[0]["filled_at"] == opened_at
        assert rows[0]["closed_at"] == "2026-08-12T18:41:36.451661+00:00"
        assert rows[0]["exit_reason"] == "Stop Loss"

    def test_metrics_holding_duration_from_opened_at(self, paper_dir: Any):
        """MetricsManager holding_hours = closed_at - opened_at (real hold)."""
        opened_at = "2026-08-12T14:47:47.816460+00:00"
        closed_at = "2026-08-12T18:41:36.451661+00:00"
        state = {
            "orders": [{
                "id": "po_1", "symbol": "HOME/IDR", "side": "SELL",
                "quantity": 535.7143, "entry_price": 168.0504,
                "fill_price": 160.9517, "exit_price": 160.9517,
                "net_pnl": -3979.13, "net_pnl_pct": -4.42,
                "status": "CLOSED",
                "created_at": opened_at, "filled_at": opened_at,
                "closed_at": closed_at, "exit_reason": "Stop Loss",
            }],
            "positions": {
                "HOME/IDR": {
                    "symbol": "HOME/IDR", "status": "CLOSED",
                    "opened_at": opened_at,
                },
            },
        }
        with open(os.path.join(str(paper_dir), "paper_state.json"), "w") as f:
            json.dump(state, f)
        with open(os.path.join(str(paper_dir), "paper_orders.json"), "w") as f:
            json.dump({"orders": [dict(state["orders"][0])]}, f)
        rebuild_trade_history_csv(str(paper_dir))

        trades = MetricsManager(str(paper_dir)).trade_history()
        assert len(trades) == 1
        t = trades[0]
        assert t["entry_time"] == opened_at
        assert t["exit_time"] == closed_at
        assert t["reason"] == "Stop Loss"
        expected_hours = (
            datetime.fromisoformat(closed_at) - datetime.fromisoformat(opened_at)
        ).total_seconds() / 3600
        assert t["holding_hours"] == pytest.approx(expected_hours, abs=0.01)
        assert t["holding_hours"] == pytest.approx(3.897, abs=0.1)

    def test_history_hold_uses_historical_timestamps(self, paper_dir: Any):
        """/history hold math resolves the real entry from position records."""
        opened_at = "2026-08-12T14:47:47.816460+00:00"
        closed_at = "2026-08-12T18:41:36.451661+00:00"

        # Post-fix CSV row: entry_time == filled_at == opened_at.
        trade = {
            "symbol": "HOME/IDR",
            "entry_time": opened_at,
            "exit_time": closed_at,
        }
        hold = order_hold_seconds(trade, {})
        assert hold is not None
        assert hold == pytest.approx(
            (datetime.fromisoformat(closed_at)
             - datetime.fromisoformat(opened_at)).total_seconds(), abs=1
        )
        # 3h 53m 49s
        assert hold == pytest.approx(3 * 3600 + 53 * 60 + 49, abs=2)

        # Legacy broken row (entry_time == exit_time == sell time) still
        # resolves the true hold via the position record's opened_at — the
        # same entry_time_map + exit-view pattern /summary and /history use.
        legacy_trade = {
            "symbol": "HOME/IDR",
            "entry_time": closed_at,
            "exit_time": closed_at,
        }
        entry_map = {"HOME/IDR": {"opened_at": opened_at}}
        hold = order_hold_seconds(
            {"symbol": "HOME/IDR", "exit_time": legacy_trade["exit_time"]},
            entry_map,
        )
        assert hold is not None
        assert hold > 3600 * 3


# ---------------------------------------------------------------------------
#  Entry indicator snapshot persistence
# ---------------------------------------------------------------------------


class TestEntrySnapshots:
    """Snapshots must be write-once and survive later pipeline runs."""

    def test_provider_buy_writes_snapshot_with_context(self, paper_dir: Any):
        provider = PaperExecutionProvider()
        _write_context(paper_dir)
        result = provider.execute_buy(_buy_request())

        snaps = load_entry_snapshots(str(paper_dir / "entry_snapshots.json"))
        assert result.order_id in snaps
        snap = snaps[result.order_id]
        assert snap["symbol"] == "BTC/USDT"
        assert snap["stop_loss"] == 49000.0
        assert snap["tp1"] == 51000.0
        assert snap["tp3"] == 53000.0
        assert snap["signal_time"] == "2026-08-12T14:47:47.560543+00:00"
        # decision scores + indicators captured at entry
        assert snap["decision"]["overall_score"] == 84.8
        assert snap["indicators"]["ema50"] == 48000.0
        assert snap["indicators"]["rsi14"] == 61.5

    def test_snapshot_immutable_across_pipeline_runs(self, paper_dir: Any):
        """Later runs must NOT mutate a previously-written snapshot."""
        provider = PaperExecutionProvider()
        _write_context(paper_dir, decision_overall=84.8, ema50=48000.0)
        result = provider.execute_buy(_buy_request())

        snap_path = str(paper_dir / "entry_snapshots.json")
        before = load_entry_snapshots(snap_path)[result.order_id]

        # Simulate the NEXT pipeline run: context files get overwritten
        # with different values, then a new BUY fill happens for the same
        # symbol (new order id).
        _write_context(paper_dir, decision_overall=12.0, ema50=99999.0)
        second = provider.execute_buy(_buy_request())

        assert second.order_id != result.order_id
        snaps = load_entry_snapshots(snap_path)
        after_old = snaps[result.order_id]
        assert after_old == before, "historical snapshot was mutated!"
        assert after_old["decision"]["overall_score"] == 84.8
        assert after_old["indicators"]["ema50"] == 48000.0
        # The NEW fill captured the NEW (later) context.
        assert snaps[second.order_id]["decision"]["overall_score"] == 12.0
        assert snaps[second.order_id]["indicators"]["ema50"] == 99999.0

    def test_save_entry_snapshot_write_once(self, paper_dir: Any):
        """Same order_id never overwrites the first snapshot."""
        snap_path = str(paper_dir / "entry_snapshots.json")
        _write_context(paper_dir)
        save_entry_snapshot(
            "BTC/USDT", "order_A", "2026-08-12T14:47:47+00:00",
            {"stop_loss": 49000.0, "tp1": 51000.0}, path=snap_path,
        )
        save_entry_snapshot(
            "BTC/USDT", "order_A", "1990-01-01T00:00:00+00:00",
            {"stop_loss": 1.0, "tp1": 2.0}, path=snap_path,
        )
        snaps = load_entry_snapshots(snap_path)
        assert snaps["order_A"]["stop_loss"] == 49000.0
        assert snaps["order_A"]["opened_at"] == "2026-08-12T14:47:47+00:00"

    def test_engine_execute_plan_writes_snapshot(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ):
        import scripts.paper_trading_engine as pte
        monkeypatch.setattr(pte, "STATE_PATH", str(tmp_path / "paper_state.json"))
        snap_path = str(tmp_path / "entry_snapshots.json")
        monkeypatch.setattr(psl, "ENTRY_SNAPSHOTS_PATH", snap_path)
        _write_context(tmp_path)

        engine = pte.PaperTradingEngine()
        plan = {
            "symbol": "BTC/USDT", "entry_price": 50000.0,
            "quantity": 0.02, "position_size_usdt": 1000.0,
            "tp1": 51000.0, "tp2": 52000.0, "tp3": 53000.0,
            "stop_loss": 49000.0, "confidence": 84.8,
            "signal_time": "2026-08-12T14:47:47.560543+00:00",
        }
        engine._execute_plan(plan, None)
        vp = engine.positions["BTC/USDT"]

        snaps = load_entry_snapshots(snap_path)
        assert vp.order_id in snaps
        snap = snaps[vp.order_id]
        assert snap["stop_loss"] == 49000.0
        assert snap["tp1"] == 51000.0
        assert snap["confidence"] == 84.8
        assert snap["decision"]["overall_score"] == 84.8
        assert snap["indicators"]["ema50"] == 48000.0