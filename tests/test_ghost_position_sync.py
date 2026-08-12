"""Regression tests for the ghost-position bug.

Scenario (production incident): ``data/paper_state.json`` contains only
PRIME/IDR, but ``data/positions.json`` still holds a stale BTC/USDT
record (``order_id=o1``, ``opened_at=2026-01-01``, ``tp1=tp2=tp3=0``,
``stop_loss=0``) left behind by an older session. Telegram ``/positions``
reads ``positions.json`` and shows BOTH.

Root cause: every ``positions.json`` writer (paper engine ``_save_state``,
pipeline write-ahead, position monitor, exit gate, order manager) MERGES
by symbol and never removes. A record whose symbol is absent from the
authoritative ``paper_state.json`` therefore survives a state reset and
every restart forever.

Fix: ``scripts.paper_state_lock.sync_positions_from_state()`` strictly
reconciles ``positions.json`` against ``paper_state.json`` — drops ghost
symbols, adds engine positions that are missing, and recomputes
``total_positions`` / ``active_count`` / ``closed_count`` — and ``main.py``
calls it once at startup after the paper engine restores state.

These tests never touch the bot's live ``data/`` dir: every ``data/...``
path is redirected into ``tmp_path/data``.
"""

import json
import os
from typing import Any

import pytest

import scripts.paper_state_lock as psl


@pytest.fixture(autouse=True)
def _isolated_data_dir(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect every ``data/...`` path into ``tmp_path/data``."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    # The incident scenario runs on an IDR account: PRIME/IDR is the real
    # position and BTC/USDT is the legacy ghost. The engine's _load_state
    # now drops positions whose quote differs from the account currency,
    # so this must be an IDR account for the real position to survive.
    monkeypatch.setenv("QUOTE_CURRENCY", "IDR")


def _write(path: Any, data: Any) -> None:
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _read(path: Any) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _prime_idr() -> dict[str, Any]:
    """positions.json-style record for PRIME/IDR (monitor-enriched)."""
    return {
        "symbol": "PRIME/IDR",
        "order_id": "po_4b20f2a1033a",
        "quantity": 11.3173,
        "remaining_qty": 7.92211,
        "entry_price": 4419.3254,
        "current_price": 4612.0,
        "unrealized_pnl": 1491.38,
        "realized_pnl": 298.57,
        "total_pnl": 1789.95,
        "cost_basis": 50064.84618076942,
        "status": "OPEN",
        "opened_at": "2026-08-04T21:23:11.399571+00:00",
        "closure_notified": False,
        "tp1": 4517.55520928,
        "tp2": 4617.11041856,
        "tp3": 4716.66562784,
        "stop_loss": 4318.44479072,
        "tp1_hit": True,
    }


def _prime_state_vp() -> dict[str, Any]:
    """VirtualPosition-shaped record as persisted in paper_state.json
    (engine vocabulary: tpX_sold, no tp1_hit)."""
    vp = dict(_prime_idr())
    vp.pop("tp1_hit")
    vp["tp1_sold"] = False
    vp["tp2_sold"] = False
    vp["tp3_sold"] = False
    vp["position_size_usdt"] = 0.0
    vp["signal_time"] = "2026-08-04T21:23:11.290706+00:00"
    return vp


def _ghost_btc() -> dict[str, Any]:
    """Legacy BTC/USDT record that paper_state.json knows nothing about."""
    return {
        "symbol": "BTC/USDT",
        "order_id": "o1",
        "quantity": 0.02,
        "remaining_qty": 0.014,
        "entry_price": 50000.0,
        "current_price": 64337.27929,
        "unrealized_pnl": 200.72,
        "realized_pnl": 30.0,
        "total_pnl": 230.72,
        "cost_basis": 1000.0,
        "status": "OPEN",
        "opened_at": "2026-01-01T00:00:00",
        "closure_notified": False,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "stop_loss": 0.0,
    }


def _paper_state(positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "balance": 965253.1773458612,
        "initial_balance": 1000000.0,
        "margin_used": 0.0,
        "orders": [],
        "positions": positions,
        "equity_history": [],
    }


def _sync_paths(tmp_path: Any) -> tuple[str, str]:
    state_path = str(tmp_path / "data" / "paper_state.json")
    positions_path = str(tmp_path / "data" / "positions.json")
    return state_path, positions_path


class TestSyncPositionsFromState:
    """sync_positions_from_state() must drop positions.json-only records."""

    def test_drops_ghost_symbol_not_in_paper_state(self, tmp_path: Any) -> None:
        """The reported bug: positions.json has BTC/USDT (order_id=o1) that
        paper_state.json does not know about — only PRIME/IDR may survive."""
        state_path, positions_path = _sync_paths(tmp_path)
        _write(state_path, _paper_state({"PRIME/IDR": _prime_idr()}))
        _write(positions_path, {
            "generated": "2026-08-04T21:23:11+00:00",
            "total_positions": 1,  # stale counter from an old writer
            "active_count": 2,
            "closed_count": 0,
            "positions": [_prime_idr(), _ghost_btc()],
        })

        changed = psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        assert changed is True
        data = _read(positions_path)
        symbols = [p["symbol"] for p in data["positions"]]
        assert symbols == ["PRIME/IDR"], f"ghost survived: {symbols}"
        # Counters must match the position list.
        assert data["total_positions"] == 1
        assert data["active_count"] == 1
        assert data["closed_count"] == 0

    def test_keeps_closed_positions_known_to_paper_state(self, tmp_path: Any) -> None:
        """CLOSED VirtualPositions ARE part of paper_state.json and must
        survive — only truly unknown symbols are ghosts."""
        state_path, positions_path = _sync_paths(tmp_path)
        prime = _prime_idr()
        prime["status"] = "CLOSED"
        prime["remaining_qty"] = 0.0
        _write(state_path, _paper_state({"PRIME/IDR": prime}))
        _write(positions_path, {"positions": [prime, _ghost_btc()]})

        psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        data = _read(positions_path)
        symbols = [p["symbol"] for p in data["positions"]]
        assert symbols == ["PRIME/IDR"]
        assert data["positions"][0]["status"] == "CLOSED"
        assert data["total_positions"] == 1
        assert data["active_count"] == 0
        assert data["closed_count"] == 1

    def test_missing_paper_state_drops_all_positions(self, tmp_path: Any) -> None:
        """After a full state reset no positions.json record may survive."""
        state_path, positions_path = _sync_paths(tmp_path)
        _write(positions_path, {"positions": [_ghost_btc()]})

        changed = psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        assert changed is True
        data = _read(positions_path)
        assert data["positions"] == []
        assert data["total_positions"] == 0
        assert data["active_count"] == 0
        assert data["closed_count"] == 0

    def test_refreshes_stale_counters_when_symbols_match(self, tmp_path: Any) -> None:
        """Even with no ghost present, stale counters are repaired."""
        state_path, positions_path = _sync_paths(tmp_path)
        _write(state_path, _paper_state({"PRIME/IDR": _prime_idr()}))
        _write(positions_path, {
            "positions": [_prime_idr()],
            "total_positions": 99,
            "active_count": 0,
            "closed_count": 99,
        })

        changed = psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        assert changed is True
        data = _read(positions_path)
        assert data["total_positions"] == 1
        assert data["active_count"] == 1
        assert data["closed_count"] == 0

    def test_adds_engine_positions_missing_from_positions_json(self, tmp_path: Any) -> None:
        """paper_state.json symbols missing from positions.json are added."""
        state_path, positions_path = _sync_paths(tmp_path)
        _write(state_path, _paper_state({"PRIME/IDR": _prime_idr()}))
        _write(positions_path, {"positions": []})

        psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        data = _read(positions_path)
        assert [p["symbol"] for p in data["positions"]] == ["PRIME/IDR"]
        assert data["total_positions"] == 1

    def test_noop_when_fully_consistent(self, tmp_path: Any) -> None:
        """A consistent file is left untouched (no pointless rewrite)."""
        state_path, positions_path = _sync_paths(tmp_path)
        _write(state_path, _paper_state({"PRIME/IDR": _prime_idr()}))
        _write(positions_path, {
            "positions": [_prime_idr()],
            "total_positions": 1,
            "active_count": 1,
            "closed_count": 0,
        })

        changed = psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )

        assert changed is False


class TestGhostSurvivesRestartWithoutSync:
    """The merge-only writers (engine _save_state) do NOT remove ghosts —
    proving the startup sync is the required removal path."""

    def test_engine_restart_merge_keeps_ghost_until_sync(self, tmp_path: Any, monkeypatch: Any) -> None:
        """A restart flow: the paper engine restores PRIME/IDR from
        paper_state.json and merges it into positions.json — the stale
        BTC/USDT record survives that save. Only sync_positions_from_state()
        removes it."""
        import scripts.paper_trading_engine as pte

        state_path, positions_path = _sync_paths(tmp_path)
        _write(state_path, _paper_state({"PRIME/IDR": _prime_state_vp()}))
        _write(positions_path, {"positions": [_prime_idr(), _ghost_btc()]})
        monkeypatch.setattr(pte, "STATE_PATH", state_path)
        monkeypatch.setattr(pte, "POSITIONS_PATH", positions_path)

        engine = pte.PaperTradingEngine()
        engine.run()

        # The engine's own save MERGES by symbol — the ghost survives.
        data = _read(positions_path)
        symbols = [p["symbol"] for p in data["positions"]]
        assert "BTC/USDT" in symbols

        # Startup reconcile is the removal path.
        changed = psl.sync_positions_from_state(
            state_path=state_path, positions_path=positions_path,
        )
        assert changed is True
        data = _read(positions_path)
        assert [p["symbol"] for p in data["positions"]] == ["PRIME/IDR"]


class TestMergePositionsCounters:
    """merge_positions() must keep total_positions consistent too."""

    def test_merge_positions_maintains_total_positions(self, tmp_path: Any) -> None:
        _write(tmp_path / "data" / "positions.json", {
            "positions": [_prime_idr()],
            "total_positions": 1,
            "active_count": 1,
            "closed_count": 0,
        })

        psl.merge_positions([_ghost_btc()])

        data = _read(tmp_path / "data" / "positions.json")
        assert data["total_positions"] == 2
        assert data["active_count"] == 2
        assert data["closed_count"] == 0
