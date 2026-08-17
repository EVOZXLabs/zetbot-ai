"""Regression tests: LIVE-mode account stability + ghost-position pruning.

Root causes fixed (from live session logs):
  * HEALTH/``/status`` net_pnl flapped between a stale paper figure and
    ``0 − initial`` (wallet fetch failure → cash 0, and initial coming
    from junk ``paper_state.json`` with a test initial_balance=10000).
  * A rejected LIVE BUY (invalid pair / maintenance / blacklist) left an
    OPEN ghost in ``positions.json`` because the Position stage simulates
    every READY plan as OPEN before execution — inflating open-position
    counts and making TP/SL reconciliation chase non-existent balances.

Covers:
  * MetricsManager ``_live_initial_balance``: snapshots once, persists,
    never reads ``paper_state.json`` in LIVE mode
  * Pipeline ``_prune_live_ghost_positions``: closes OPEN entries with no
    exchange backing, keeps real + EXCLUDE_SYMBOLS holdings
  * ``_LiveWalletAdapter`` holds last known balance on transient failure
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _write_positions(tmp_path, entries: list[dict]) -> None:
    (tmp_path / "data").mkdir(exist_ok=True)
    with open(tmp_path / "data" / "positions.json", "w") as f:
        json.dump({"positions": entries}, f)


# ---------------------------------------------------------------------------
#  LIVE initial-equity baseline
# ---------------------------------------------------------------------------


class TestLiveInitialBalance:
    def test_persists_once_and_is_stable(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        wallet = SimpleNamespace(equity=496672.0)

        from scripts.metrics_manager import MetricsManager

        m = MetricsManager("data", wallet=wallet, mode_provider=lambda: "LIVE")
        first = m._live_initial_balance()
        assert first == pytest.approx(496672.0)

        path = tmp_path / "data" / "live_initial_balance.json"
        assert path.exists()
        with open(path) as f:
            record = json.load(f)
        assert record["initial_balance"] == pytest.approx(496672.0)

        # A second call (even with a different wallet value) must NOT
        # re-snapshot — the baseline is anchored to first live start.
        second = MetricsManager("data", wallet=SimpleNamespace(equity=1.0),
                                mode_provider=lambda: "LIVE")._live_initial_balance()
        assert second == pytest.approx(496672.0)

    def test_falls_back_to_account_balance_when_wallet_unavailable(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        monkeypatch.setenv("ACCOUNT_BALANCE", "300000")

        from scripts.metrics_manager import MetricsManager

        m = MetricsManager("data", wallet=None, mode_provider=lambda: "LIVE")
        assert m._live_initial_balance() == pytest.approx(300000.0)

    def test_live_never_reads_paper_state_initial(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        # Junk paper_state with a test initial_balance — must be ignored
        # in LIVE mode (this is what caused HEALTH net_pnl = -10000).
        with open(tmp_path / "data" / "paper_state.json", "w") as f:
            json.dump({"initial_balance": 10000, "balance": 9000}, f)

        from scripts.metrics_manager import MetricsManager

        m = MetricsManager("data", wallet=SimpleNamespace(equity=496672.0),
                           mode_provider=lambda: "LIVE")
        assert m._live_initial_balance() == pytest.approx(496672.0)


# ---------------------------------------------------------------------------
#  LIVE ghost-position pruning
# ---------------------------------------------------------------------------


class TestLiveGhostPrune:
    def _pipeline(self, tmp_path):
        from scripts.pipeline import Pipeline

        logger = MagicMock()
        cfg = SimpleNamespace(
            quote_currency="IDR",
            data_dir=str(tmp_path / "data"),
            exchange="indodax",
            timeframe="1h",
        )
        p = Pipeline.__new__(Pipeline)
        p.logger = logger
        p.config = cfg
        return p

    def test_rejected_buy_ghost_is_closed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_positions(tmp_path, [
            {"symbol": "SYN/IDR", "status": "OPEN", "remaining_qty": 104.0},
            {"symbol": "BTC/IDR", "status": "OPEN", "remaining_qty": 0.001},
        ])
        # Exchange truth: only BTC/IDR actually exists on the account.
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({
            "BTC/IDR": {"symbol": "BTC/IDR", "quantity": 0.001, "entry_price": 1121566000},
        }))

        self._pipeline(tmp_path)._prune_live_ghost_positions()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = {p["symbol"]: p for p in json.load(f)["positions"]}
        assert entries["SYN/IDR"]["status"] == "CLOSED"
        assert entries["SYN/IDR"]["remaining_qty"] == 0.0
        assert entries["BTC/IDR"]["status"] == "OPEN"

    def test_excluded_symbol_never_touched(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EXCLUDE_SYMBOLS", "RFC,VRA")
        _write_positions(tmp_path, [
            {"symbol": "RFC/IDR", "status": "OPEN", "remaining_qty": 1062.0},
        ])
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({}))

        self._pipeline(tmp_path)._prune_live_ghost_positions()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = json.load(f)["positions"]
        assert entries[0]["status"] == "OPEN"

    def test_closed_entries_untouched(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_positions(tmp_path, [
            {"symbol": "OLD/IDR", "status": "CLOSED", "remaining_qty": 0.0},
        ])
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({}))

        self._pipeline(tmp_path)._prune_live_ghost_positions()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = json.load(f)["positions"]
        assert entries[0]["status"] == "CLOSED"

    def test_just_filled_position_never_pruned(self, tmp_path, monkeypatch) -> None:
        """Regression: a BUY that FILLED this cycle must survive the prune.

        The prune runs in the SAME pipeline cycle as the BUY, but
        live_positions.json is resynced at the START of the cycle (before
        the BUY) — so a just-filled position is not yet in it. Pruning
        against that stale snapshot killed a real filled KOMA position 2s
        after a successful BUY (the coins stayed on the exchange but the
        position was marked CLOSED and never managed/sold).
        """
        monkeypatch.chdir(tmp_path)
        _write_positions(tmp_path, [
            {"symbol": "KOMA/IDR", "status": "OPEN", "remaining_qty": 782.0},
        ])
        # Stale exchange snapshot from BEFORE the BUY — KOMA absent.
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({}))
        # No exchange_manager passed => no live balance check (test env).

        self._pipeline(tmp_path)._prune_live_ghost_positions(
            recently_filled={"KOMA/IDR"},
        )

        with open(tmp_path / "data" / "positions.json") as f:
            entries = json.load(f)["positions"]
        assert entries[0]["status"] == "OPEN"
        assert entries[0]["remaining_qty"] == 782.0

    def test_position_held_on_exchange_never_pruned(self, tmp_path, monkeypatch) -> None:
        """Even without the filled-set, an OPEN entry whose base asset has
        a non-zero exchange balance is real, not a ghost."""
        monkeypatch.chdir(tmp_path)
        _write_positions(tmp_path, [
            {"symbol": "KOMA/IDR", "status": "OPEN", "remaining_qty": 782.0},
        ])
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({}))

        class _EM:
            class _P:
                @staticmethod
                def fetch_balance():
                    return {"free": {"KOMA": 782.0, "IDR": 301003.0}}
            def get_provider(self):
                return self._P()

        self._pipeline(tmp_path)._prune_live_ghost_positions(
            exchange_manager=_EM(),
        )

        with open(tmp_path / "data" / "positions.json") as f:
            entries = json.load(f)["positions"]
        assert entries[0]["status"] == "OPEN"


class TestLiveAdoption:
    """Exchange-held holdings without a plan entry must still be tracked
    and TP/SL-managed, or a buy whose record was lost (prune bug) or a
    pre-arming holding is never sold."""

    def _pipeline(self, tmp_path):
        from scripts.pipeline import Pipeline

        logger = MagicMock()
        cfg = SimpleNamespace(
            quote_currency="IDR",
            data_dir=str(tmp_path / "data"),
            exchange="indodax",
            timeframe="1h",
        )
        p = Pipeline.__new__(Pipeline)
        p.logger = logger
        p.config = cfg
        return p

    def test_exchange_holding_is_adopted_into_managed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "positions.json").write_text(json.dumps(
            {"positions": [], "active_count": 0, "closed_count": 0},
        ))
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({
            "KOMA/IDR": {
                "symbol": "KOMA/IDR", "quantity": 782.0,
                "entry_price": 248.587, "current_price": 242.304,
                "stop_loss": 243.6315, "tp1": 253.5425,
                "source": "live_exchange_sync",
            },
        }))

        self._pipeline(tmp_path)._merge_live_positions_into_managed()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = {p["symbol"]: p for p in json.load(f)["positions"]}
        k = entries["KOMA/IDR"]
        assert k["status"] == "OPEN"
        assert k["remaining_qty"] == 782.0
        assert k["entry_price"] == 248.587
        assert k["stop_loss"] == 243.6315
        assert k["tp1"] == 253.5425

    def test_managed_entries_win_over_re_adoption(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "positions.json").write_text(json.dumps(
            {"positions": [{
                "symbol": "KOMA/IDR", "status": "OPEN", "remaining_qty": 782.0,
                "stop_loss": 200.0, "entry_price": 250.0,
            }]},
        ))
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({
            "KOMA/IDR": {"symbol": "KOMA/IDR", "quantity": 782.0,
                         "entry_price": 248.587, "current_price": 242.304},
        }))

        self._pipeline(tmp_path)._merge_live_positions_into_managed()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = json.load(f)["positions"]
        assert len(entries) == 1
        assert entries[0]["stop_loss"] == 200.0

    def test_adoption_restores_levels_from_entry_snapshot(self, tmp_path, monkeypatch) -> None:
        # A live holding with NO levels in the live cache must still get
        # stop/tp restored from the write-once entry snapshot store —
        # otherwise every restart wipes TP/SL for non-plan holdings.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "positions.json").write_text(json.dumps(
            {"positions": [], "active_count": 0, "closed_count": 0},
        ))
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({
            "WLFI/IDR": {
                "symbol": "WLFI/IDR", "quantity": 221.43266367,
                "entry_price": 1086.957976320495, "current_price": 1064.0,
                "source": "live_exchange_sync",
            },
        }))
        (tmp_path / "data" / "entry_snapshots.json").write_text(json.dumps({
            "generated": "t",
            "snapshots": {
                "LIVE-1": {
                    "order_id": "LIVE-1", "symbol": "WLFI/IDR",
                    "stop_loss": 1055.02220148, "tp1": 1118.89375116,
                    "tp2": 1150.82952601, "tp3": 1182.76530086,
                },
            },
        }))

        self._pipeline(tmp_path)._merge_live_positions_into_managed()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = {p["symbol"]: p for p in json.load(f)["positions"]}
        w = entries["WLFI/IDR"]
        assert w["status"] == "OPEN"
        assert w["stop_loss"] == 1055.02220148
        assert w["tp1"] == 1118.89375116
        assert w["tp2"] == 1150.82952601
        assert w["tp3"] == 1182.76530086

    def test_managed_entry_heals_levels_from_entry_snapshot(self, tmp_path, monkeypatch) -> None:
        # Managed entry whose stop/TP were zeroed by an old buggy sync
        # must self-heal from the write-once entry snapshot store.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "positions.json").write_text(json.dumps(
            {"positions": [{
                "symbol": "WLFI/IDR", "status": "OPEN", "remaining_qty": 221.43,
                "stop_loss": 0.0, "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
                "entry_price": 1086.957976320495,
            }]},
        ))
        (tmp_path / "data" / "live_positions.json").write_text(json.dumps({
            "WLFI/IDR": {"symbol": "WLFI/IDR", "quantity": 221.43,
                         "entry_price": 1086.957976320495, "current_price": 1064.0},
        }))
        (tmp_path / "data" / "entry_snapshots.json").write_text(json.dumps({
            "generated": "t",
            "snapshots": {
                "LIVE-1": {
                    "order_id": "LIVE-1", "symbol": "WLFI/IDR",
                    "stop_loss": 1055.02220148, "tp1": 1118.89375116,
                    "tp2": 1150.82952601, "tp3": 1182.76530086,
                },
            },
        }))

        self._pipeline(tmp_path)._merge_live_positions_into_managed()

        with open(tmp_path / "data" / "positions.json") as f:
            entries = {p["symbol"]: p for p in json.load(f)["positions"]}
        w = entries["WLFI/IDR"]
        assert w["stop_loss"] == 1055.02220148
        assert w["tp1"] == 1118.89375116
        assert w["tp2"] == 1150.82952601
        assert w["tp3"] == 1182.76530086


# ---------------------------------------------------------------------------
#  Wallet holds last known balance on transient failure
# ---------------------------------------------------------------------------


class TestWalletHoldOnFailure:
    def _wallet(self, config=None):
        from scripts import service_container as sc

        class _Cfg:
            quote_currency = "IDR"

        class _Ex:
            def __init__(self, provider):
                self._provider = provider
            def get_provider(self):
                return self._provider

        class _Fail:
            def __init__(self):
                self.calls = 0
            def fetch_balance(self):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "free": {"IDR": 496672.0},
                        "total": {"IDR": 496672.0},
                    }
                raise RuntimeError("exchange down")

        ex = _Ex(_Fail())
        w = sc._LiveWalletAdapter(_Cfg(), ex)
        assert w.balance == pytest.approx(496672.0)
        # transient failure now reuses the last known snapshot
        assert w.balance == pytest.approx(496672.0)

    def test_first_fetch_failure_still_raises(self, monkeypatch) -> None:
        from scripts import service_container as sc

        class _Cfg:
            quote_currency = "IDR"

        class _Ex:
            def get_provider(self):
                class _P:
                    def fetch_balance(self):
                        raise RuntimeError("exchange down")
                return _P()

        with pytest.raises(RuntimeError):
            sc._LiveWalletAdapter(_Cfg(), _Ex()).balance
