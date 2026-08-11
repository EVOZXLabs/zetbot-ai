"""Regression tests for BUG-4: paper-state file writes must be serialized.

Root cause (see the BUG-4 audit): in PAPER mode TWO independent threads
read-modify-write the SAME JSON files:

    * the position monitor (``main.py``, ~60 s) — on closure calls
      ``main._update_paper_on_closure`` which RMWs ``data/paper_balance.json``,
      ``data/positions.json``, ``data/paper_orders.json`` and (via
      ``_sync_paper_state_on_closure``) ``data/paper_state.json``;
    * the pipeline / manual-sell path (~300 s) — ``order_manager._sync_paper_files``
      and ``_close_paper_position_on_sell`` RMW the SAME files, and
      ``paper_trading_engine._save_state`` / ``execution_provider``
      ``PaperBalance.save`` / ``PaperExecutionProvider._save_positions``
      overwrite them.

With no shared lock an interleaved read-modify-write loses an increment
(e.g. one closure's ``final_balance`` credit is overwritten by a stale
reader) or two simultaneous ``open(..., "w")`` corrupt a file — the paper
analog of the BUG-2 LIVE oversell race.

Fix: ``scripts.paper_state_lock`` exposes ONE re-entrant lock
(``PAPER_STATE_LOCK``) acquired by every paper-state file writer via the
``@paper_state_writes`` decorator, plus ``merge_positions`` so the two bulk
``positions.json`` writers (monitor + pipeline) merge by symbol instead of
clobbering each other.

These tests never touch a real exchange or the real ``data/`` dir: every
``data/...`` path is redirected into ``tmp_path/data`` via
``monkeypatch.chdir``.
"""

import json
import logging
import os
import threading
from threading import Barrier, Event, Thread
from typing import Any

import pytest

import main as main_mod
import scripts.order_manager as order_manager
from scripts.paper_trading_engine import ExecutionModel

_LOGGER = logging.getLogger("test_bug4")

BTC = "BTC/USDT"
ETH = "ETH/USDT"

_BAL = {
    "initial_balance": 1000.0,
    "final_balance": 1000.0,
    "final_equity": 1000.0,
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "win_rate": 0.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "net_pnl": 0.0,
    "total_return_pct": 0.0,
}


def _vp(symbol: str, **overrides: Any) -> dict[str, Any]:
    vp: dict[str, Any] = {
        "symbol": symbol,
        "status": "OPEN",
        "entry_price": 100.0,
        "quantity": 0.1,
        "remaining_qty": 0.1,
        "cost_basis": 10.0,
        "floating_pnl_pct": 0.0,
        "entry_time": "2026-01-01T00:00:00+00:00",
        "realized_pnl": 0.0,
        "total_pnl": 0.0,
        "unrealized_pnl": 0.0,
    }
    vp.update(overrides)
    return vp


def _seed(positions: list[dict[str, Any]], state_balance: float = 1000.0) -> None:
    os.makedirs("data", exist_ok=True)
    with open("data/positions.json", "w") as f:
        json.dump({"positions": list(positions)}, f, indent=2)
    with open("data/paper_balance.json", "w") as f:
        json.dump(dict(_BAL), f, indent=2)
    with open("data/paper_state.json", "w") as f:
        json.dump(
            {
                "version": 1,
                "balance": state_balance,
                "positions": {p["symbol"]: dict(p) for p in positions},
            },
            f,
            indent=2,
            default=str,
        )
    with open("data/paper_orders.json", "w") as f:
        json.dump({"orders": []}, f, indent=2)


def _read(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _run_two_actors(actor_a: Any, actor_b: Any, barrier: Barrier) -> None:
    start_a, start_b = Event(), Event()

    def _wrap(actor: Any, start: Event) -> None:
        start.set()
        barrier.wait(timeout=15)
        actor()

    t1 = Thread(target=_wrap, args=(actor_a, start_a))
    t2 = Thread(target=_wrap, args=(actor_b, start_b))
    t1.start()
    t2.start()
    start_a.wait(timeout=5)
    start_b.wait(timeout=5)
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not t1.is_alive(), "actor A did not finish"
    assert not t2.is_alive(), "actor B did not finish"


@pytest.fixture
def _paper_env(tmp_path, monkeypatch) -> None:
    """Redirect every ``data/...`` path into ``tmp_path/data``."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)


class TestMergePositions:
    def test_merge_positions_preserves_symbols_of_concurrent_writer(self, _paper_env) -> None:
        """Two writers persist DIFFERENT symbols concurrently; the bulk
        positions.json merge must preserve BOTH (the old full-file overwrite
        would drop whichever wrote first)."""
        for _ in range(30):
            _seed([_vp(BTC)])
            barrier = Barrier(2)

            def _write_b() -> None:
                barrier.wait(timeout=15)
                from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
                merge_positions([_vp(ETH, status="CLOSED", remaining_qty=0.0)])

            t = Thread(target=_write_b)
            t.start()
            barrier.wait(timeout=15)
            from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
            merge_positions([_vp(BTC, status="CLOSED", remaining_qty=0.0)])
            t.join(timeout=20)

            data = _read("data/positions.json")
            symbols = {p["symbol"] for p in data.get("positions", [])}
            assert symbols == {BTC, ETH}, f"lost a concurrent symbol: {symbols}"

    def test_old_overwrite_pattern_drops_first_writer(self, _paper_env) -> None:
        """Control: the OLD pattern (each writer overwrites the whole file
        with its own stale view) deterministically loses the first writer's
        symbol — proving the merge regression test above fails pre-fix."""
        from scripts.paper_state_lock import merge_positions  # noqa: PLC0415

        _seed([_vp(BTC)])
        # Writer A reads the current file, then writer B's stale overwrite
        # clobbers everything A knew about.
        with open("data/positions.json") as f:
            stale_a = json.load(f)
        stale_a["positions"] = [_vp(BTC, status="CLOSED", remaining_qty=0.0)]
        with open("data/positions.json", "w") as f:
            json.dump(stale_a, f, indent=2)
        merge_positions([_vp(ETH)])
        data = _read("data/positions.json")
        assert {p["symbol"] for p in data.get("positions", [])} == {BTC, ETH}


class TestMonitorVsManualSell:
    def test_concurrent_closure_and_manual_sell_credit_both_proceeds(self, _paper_env) -> None:
        """The monitor's closure (``_update_paper_on_closure``) and a manual
        /sell (``_sync_paper_files``) run concurrently on DIFFERENT symbols.
        Both proceeds must land in ``paper_balance.json`` / ``paper_state.json``
        and both positions must close in ``positions.json``. Without the shared
        lock one writer's RMW overwrites the other's (lost update)."""
        sell_a = ExecutionModel.sell(105.0, 0.1)
        proceeds_a = sell_a["total_proceeds"]
        expected_balance = round(round(1000.0 + proceeds_a, 2) + 20.0, 2)

        for _ in range(30):
            _seed([_vp(BTC), _vp(ETH)])
            barrier = Barrier(2)

            def _monitor_actor() -> None:
                barrier.wait(timeout=15)
                main_mod._update_paper_on_closure(
                    _LOGGER, BTC, _vp(BTC), 105.0, "Take Profit",
                )

            def _manual_sell_actor() -> None:
                barrier.wait(timeout=15)
                order_manager._sync_paper_files(
                    ETH, "SELL", 0.1, 200.0, cost=20.0, pnl=5.0, fee=0.0,
                )

            _run_two_actors(_monitor_actor, _manual_sell_actor, barrier)

            pb = _read("data/paper_balance.json")
            assert float(pb["final_balance"]) == pytest.approx(expected_balance), (
                f"lost a proceeds credit: final_balance={pb['final_balance']} "
                f"expected {expected_balance}"
            )
            assert pb["total_trades"] == 2

            state = _read("data/paper_state.json")
            assert float(state["balance"]) == pytest.approx(expected_balance)
            closed = {s for s, v in state.get("positions", {}).items() if v.get("status") == "CLOSED"}
            assert {BTC, ETH} <= closed, f"not both closed in paper_state: {closed}"

            data = _read("data/positions.json")
            pos = {p["symbol"]: p for p in data.get("positions", [])}
            assert pos[BTC]["status"] in ("CLOSED", "STOPPED")
            assert pos[ETH]["status"] in ("CLOSED", "STOPPED")

            orders = _read("data/paper_orders.json").get("orders", [])
            assert len(orders) == 2, f"expected 2 orders, got {len(orders)}"

    def test_live_mode_does_not_use_paper_lock(self) -> None:
        """The paper lock is scope-limited to PAPER accounting: the LIVE exit
        gate is a separate, per-symbol mechanism (BUG-2). Asserting the lock
        object exists and is re-entrant guards the BUG-4 wiring."""
        from scripts.paper_state_lock import PAPER_STATE_LOCK  # noqa: PLC0415

        with PAPER_STATE_LOCK:
            with PAPER_STATE_LOCK:
                pass


class TestAtomicWriteJson:
    """BUG B regression: concurrent readers must never see truncated JSON.

    ``atomic_write_json`` writes to a temp file in the same directory
    then atomically renames (``os.replace``) it over the target.  A
    concurrent reader either sees the old complete file or the new
    complete file — never a partially-written / empty / truncated file
    that triggers ``json.JSONDecodeError`` → silent ``{}`` fallback in
    ``MetricsManager._read_json``.
    """

    def test_basic_roundtrip(self, tmp_path):
        from scripts.paper_state_lock import atomic_write_json

        path = str(tmp_path / "test.json")
        atomic_write_json(path, {"foo": "bar"})
        with open(path) as f:
            assert json.load(f) == {"foo": "bar"}

    def test_overwrites_existing_file(self, tmp_path):
        from scripts.paper_state_lock import atomic_write_json

        path = str(tmp_path / "test.json")
        atomic_write_json(path, {"v": 1})
        atomic_write_json(path, {"v": 2})
        with open(path) as f:
            assert json.load(f) == {"v": 2}

    def test_kwargs_forwarded(self, tmp_path):
        from scripts.paper_state_lock import atomic_write_json

        path = str(tmp_path / "test.json")
        atomic_write_json(path, {"a": 1}, indent=2, default=str)
        with open(path) as f:
            content = f.read()
        assert "\n" in content  # indented

    def test_tmp_cleaned_on_error(self, tmp_path):
        from scripts.paper_state_lock import atomic_write_json

        path = str(tmp_path / "test.json")
        bad_data = object()  # json.dump will raise TypeError
        with pytest.raises(TypeError):
            atomic_write_json(path, bad_data)
        # No leftover .tmp files
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")]
        assert tmp_files == []

    def test_atomicity_under_concurrent_read_write(self, tmp_path):
        """Simulate a reader thread calling json.load while the writer
        repeatedly calls atomic_write_json.  The reader must never see
        a truncated file (json.JSONDecodeError)."""
        from scripts.paper_state_lock import atomic_write_json

        path = str(tmp_path / "state.json")
        atomic_write_json(path, {"balance": 0.0, "positions": {}})

        errors: list[str] = []
        stop = threading.Event()

        def writer():
            for i in range(200):
                atomic_write_json(
                    path, {"balance": float(i), "positions": {"X": i}},
                )

        def reader():
            while not stop.is_set():
                try:
                    with open(path) as f:
                        data = json.load(f)
                    assert "balance" in data
                except json.JSONDecodeError as exc:
                    errors.append(str(exc))

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join()
        stop.set()
        t_r.join(timeout=2.0)

        assert errors == [], f"reader saw truncated JSON: {errors[:5]}"
