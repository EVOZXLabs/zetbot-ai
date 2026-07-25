"""
Regression tests for portfolio-wide exposure accounting.

Bug this guards against
────────────────────────
``/wallet`` and ``/status`` reported exposure at ~100 % even though
``MAX_POSITION_SIZE_PCT`` was configured at 60 %. Root cause:
``scripts.risk_manager.main()`` (called on every pipeline cycle)
built a bare ``RiskManager()``, whose ``balance`` default falls back
to the module-level ``ACCOUNT_BALANCE`` constant (a fixed $10,000) —
never the account's *actual* current cash. Real cash shrinks as
positions are opened, but the risk engine kept "seeing" a full,
un-spent balance forever, so every pipeline cycle re-approved new
positions on top of ones already open, pushing real exposure toward
100 % regardless of the configured cap.

Covers
──────
1. A single $4,000 position against a $10,000 equity account leaves
   exactly $2,000 of headroom under a 60 % cap.
2. A second position is capped to that remaining headroom — cumulative
   exposure can never exceed ``MAX_POSITION_SIZE_PCT``.
3. Exposure already committed in a *previous* pipeline cycle (i.e.
   "open before this RiskManager instance existed") still counts
   against the cap for brand-new RiskManager instances.
4. Restarting the bot (fresh process, fresh RiskManager built from
   ``data/paper_balance.json``) reports identical exposure to before
   the restart — it is never silently reset to the hardcoded
   ``ACCOUNT_BALANCE`` constant.
5. Telegram (``MetricsManager.account()``) and the risk engine
   (``scripts.risk_manager._resolve_account_state()``) derive equity
   from the same ``paper_balance.json`` and therefore always agree.
"""

import json
import os
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import risk_manager
from scripts.risk_manager import PositionSizer, RiskManager
from scripts.metrics_manager import MetricsManager


def _write_paper_balance(**overrides) -> None:
    """Write data/paper_balance.json (cwd must already be the tmp root)."""
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
    with open("data/paper_balance.json", "w") as f:
        json.dump(data, f)


# ===========================================================================
#  1 & 2 — portfolio-wide cap math ($10,000 equity / 60 % cap example)
# ===========================================================================


class TestPortfolioWideExposureCap:
    def test_first_position_room_is_60pct_of_equity(self):
        mgr = RiskManager(
            balance=10_000.0, equity=10_000.0,
            existing_exposure=0.0, max_position_size_pct=0.6,
        )
        assert mgr._max_new_position_value() == pytest.approx(6_000.0)

    def test_second_position_capped_to_remaining_budget(self):
        """Position 1 = $4,000 -> remaining budget must be exactly $2,000."""
        mgr = RiskManager(
            balance=10_000.0, equity=10_000.0,
            existing_exposure=0.0, max_position_size_pct=0.6,
        )
        mgr._used_capital += 4_000.0  # position 1 approved

        remaining = mgr._max_new_position_value()

        assert remaining == pytest.approx(2_000.0)

    def test_position_sizer_respects_remaining_budget(self):
        """End-to-end: PositionSizer must not exceed the cap returned by
        RiskManager._max_new_position_value(), even with a wide stop
        that would otherwise size a much larger position."""
        mgr = RiskManager(
            balance=10_000.0, equity=10_000.0,
            existing_exposure=0.0, max_position_size_pct=0.6,
        )
        mgr._used_capital += 4_000.0
        cap = mgr._max_new_position_value()

        # A generous risk budget / tight stop would size ~$8,000 uncapped.
        _, _, position_value = PositionSizer.calculate(
            balance=10_000.0, risk_pct=20.0,
            entry_price=100.0, stop_price=99.75,
            max_position_value=cap,
        )

        assert position_value <= cap + 1e-9
        assert position_value == pytest.approx(2_000.0)

    def test_cumulative_exposure_never_exceeds_cap(self):
        mgr = RiskManager(
            balance=10_000.0, equity=10_000.0,
            existing_exposure=0.0, max_position_size_pct=0.6,
        )
        mgr._used_capital += 4_000.0
        mgr._used_capital += mgr._max_new_position_value()

        total_exposure_pct = mgr._used_capital / mgr.equity * 100.0

        assert total_exposure_pct == pytest.approx(60.0)
        assert mgr._used_capital <= mgr.equity * 0.6 + 1e-9

    def test_exposure_open_in_previous_cycle_reduces_new_room(self):
        """A $4,000 position already open from an *earlier* pipeline run
        must still count against a brand-new RiskManager's cap."""
        mgr = RiskManager(
            balance=6_000.0, equity=10_000.0,
            existing_exposure=4_000.0, max_position_size_pct=0.6,
        )
        assert mgr._max_new_position_value() == pytest.approx(2_000.0)


# ===========================================================================
#  3 & 4 — restart must not reset exposure accounting
# ===========================================================================


class TestRestartDoesNotChangeExposure:
    def test_resolve_account_state_reads_live_balance_not_hardcoded(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        _write_paper_balance(final_balance=6_000.0, final_equity=10_000.0)

        balance, equity = risk_manager._resolve_account_state()

        assert balance == pytest.approx(6_000.0)
        assert equity == pytest.approx(10_000.0)
        # The old bug: balance silently defaulted to ACCOUNT_BALANCE
        # (10_000.0) regardless of how much cash was actually spent.
        assert balance != pytest.approx(risk_manager.ACCOUNT_BALANCE)

    def test_restart_reports_identical_exposure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        _write_paper_balance(final_balance=6_000.0, final_equity=10_000.0)

        def exposure_pct_from_disk() -> float:
            balance, equity = risk_manager._resolve_account_state()
            return (equity - balance) / equity * 100.0

        # "Before restart"
        exposure_before = exposure_pct_from_disk()
        # "After restart" — a brand-new process/module import would call
        # _resolve_account_state() fresh; simulate that by calling it
        # again against the same persisted file.
        exposure_after = exposure_pct_from_disk()

        assert exposure_before == pytest.approx(exposure_after)
        assert exposure_after == pytest.approx(40.0)  # (10000-6000)/10000


# ===========================================================================
#  5 — Telegram and the risk engine must agree on equity
# ===========================================================================


class TestTelegramEngineEquityConsistency:
    def test_metrics_manager_and_risk_manager_agree_on_equity(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        _write_paper_balance(final_balance=6_000.0, final_equity=10_000.0)

        with open("data/positions.json", "w") as f:
            json.dump({"positions": [{
                "symbol": "BTCUSDT",
                "status": "OPEN",
                "quantity": 40.0,
                "current_price": 100.0,
                "unrealized_pnl": 0.0,
            }]}, f)

        telegram_snapshot = MetricsManager(data_dir="data").account()
        engine_balance, engine_equity = risk_manager._resolve_account_state()

        assert telegram_snapshot.equity == pytest.approx(engine_equity)
        assert telegram_snapshot.balance == pytest.approx(engine_balance)
        assert telegram_snapshot.exposure_pct == pytest.approx(40.0)
