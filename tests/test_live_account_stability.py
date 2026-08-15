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
