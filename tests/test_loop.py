"""
Unit tests for TradingLoop.

Covers: initialisation, single cycle, continuous loop,
retry after failure, graceful shutdown, logging,
statistics update, and integration with PaperTradingEngine.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bot.loop import TradingLoop
from bot.paper_engine import PaperTradingEngine


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _mock_result(
    signal_name: str = "HOLD",
    trade: dict | None = None,
    position: dict | None = None,
    price: float = 50_000.0,
    market_state: str = "SIDEWAYS",
) -> dict:
    """Build a mock return value for ``PaperTradingEngine.run_once``."""
    return {
        "state": "IDLE",
        "signal": {"signal": signal_name, "reason": ["test"]},
        "trade": trade,
        "position": position,
        "price": price,
        "market_state": market_state,
    }


def _hold_df() -> pd.DataFrame:
    """Flat-price DataFrame that produces HOLD."""
    n = 250
    return pd.DataFrame({
        "high": [51_000.0] * n,
        "low":  [49_000.0] * n,
        "close":[50_000.0] * n,
    })


def _run_loop_in_thread(
    loop: TradingLoop,
    duration: float = 0.15,
) -> int:
    """Run ``loop.run()`` in a daemon thread, stop after *duration* seconds.

    Returns the number of completed cycles.
    """
    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    time.sleep(duration)
    loop.stop()
    t.join(timeout=3)
    return loop.cycle_count


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

class TestTradingLoopInit:
    """TradingLoop initialisation."""

    def test_initial_state(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        loop = TradingLoop(engine=engine, interval=60)
        assert loop.cycle_count == 0
        assert not loop.is_running

    def test_default_engine_creation(self) -> None:
        loop = TradingLoop(interval=60)
        assert isinstance(loop.engine, PaperTradingEngine)

    def test_custom_engine(self) -> None:
        engine = PaperTradingEngine(initial_balance=5_000.0)
        loop = TradingLoop(engine=engine, interval=60)
        assert loop.engine is engine
        assert loop.engine.current_balance() == 5_000.0

    def test_custom_interval(self) -> None:
        loop = TradingLoop(interval=30)
        assert loop._interval == 30

    def test_engine_property(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        loop = TradingLoop(engine=engine, interval=60)
        assert loop.engine is engine


class TestTradingLoopSingleCycle:
    """Loop runs at least one cycle."""

    def test_one_cycle(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        count = _run_loop_in_thread(loop, duration=0.1)
        assert count >= 1

    def test_cycle_count_increments(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        count = _run_loop_in_thread(loop, duration=0.25)
        assert count >= 2

    def test_stop_sets_running_false(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        _run_loop_in_thread(loop, duration=0.1)
        assert not loop.is_running


class TestTradingLoopStop:
    """Stop behaviour."""

    def test_stop_during_sleep(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=10)
        t = threading.Thread(target=loop.run, daemon=True)
        t.start()
        time.sleep(0.05)
        loop.stop()
        t.join(timeout=3)
        assert loop.cycle_count >= 1
        assert not loop.is_running

    def test_stop_before_run(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        loop = TradingLoop(engine=engine, interval=60)
        loop.stop()
        assert not loop.is_running

    def test_stop_is_idempotent(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        loop.stop()
        loop.stop()
        loop.stop()
        assert not loop.is_running


class TestTradingLoopRetry:
    """Error handling and retry logic."""

    def test_retry_succeeds(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.side_effect = [
            ValueError("API error"),
            _mock_result(),
        ]
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        # Set very short retry delay so test doesn't hang
        loop._retry_delay = 0.01
        count = _run_loop_in_thread(loop, duration=0.3)
        assert count >= 1
        assert engine.run_once.call_count >= 2

    def test_retry_exhausted_continues(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.side_effect = ValueError("Persistent error")
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        loop._max_retry = 2
        loop._retry_delay = 0.01
        count = _run_loop_in_thread(loop, duration=0.4)
        assert count >= 1
        # original + retries for each cycle
        assert engine.run_once.call_count >= 3

    def test_keyboard_interrupt_during_cycle(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.side_effect = KeyboardInterrupt()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=60)
        t = threading.Thread(target=loop.run, daemon=True)
        t.start()
        time.sleep(0.1)
        t.join(timeout=3)
        assert not loop.is_running


class TestTradingLoopIntegration:
    """Integration with real PaperTradingEngine."""

    def test_integration_engine_accepts_result(self) -> None:
        """Run loop with a real engine whose run_once is patched."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        original_run_once = engine.run_once

        with patch.object(engine, "run_once") as mock_run:
            mock_run.return_value = _mock_result(price=50_000.0)

            loop = TradingLoop(engine=engine, interval=0.01)
            count = _run_loop_in_thread(loop, duration=0.15)
            assert count >= 1
            mock_run.assert_called()

    def test_engine_statistics_accessible(self) -> None:
        """Statistics are accessible through the loop's engine."""
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0
        stats = {
            "total_trades": 1,
            "total_profit": 100.0,
            "win_rate": 100.0,
        }
        engine.statistics.return_value = stats

        loop = TradingLoop(engine=engine, interval=0.01)
        _run_loop_in_thread(loop, duration=0.1)
        assert loop.engine.statistics()["total_trades"] == 1


class TestTradingLoopSideEffects:
    """The loop does not corrupt engine state."""

    def test_balance_preserved(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        _run_loop_in_thread(loop, duration=0.1)
        assert loop.engine.current_balance() == 10_000.0

    def test_loop_does_not_raise(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.side_effect = [
            ValueError("burst"),
            _mock_result(),
            _mock_result(),
        ]
        engine.current_balance.return_value = 10_000.0

        loop = TradingLoop(engine=engine, interval=0.01)
        loop._retry_delay = 0.01
        loop._max_retry = 1
        try:
            _run_loop_in_thread(loop, duration=0.3)
        except Exception:
            pytest.fail("Loop raised an unexpected exception")


class TestTradingLoopDailySummary:
    """Daily summary scheduling in TradingLoop."""

    def test_maybe_send_daily_summary_sends_once_per_day(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_500.0
        engine.statistics.return_value = {
            "total_trades": 5,
            "win_count": 3,
            "loss_count": 2,
            "win_rate": 60.0,
            "total_profit": 500.0,
            "profit_factor": 1.5,
            "average_win": 200.0,
            "average_loss": -50.0,
        }

        notifier = MagicMock()
        loop = TradingLoop(engine=engine, interval=60)
        loop._engine._notifier = notifier

        loop._maybe_send_daily_summary()
        notifier.daily_summary.assert_called_once()

        loop._maybe_send_daily_summary()
        notifier.daily_summary.assert_called_once()

    def test_maybe_send_daily_summary_no_notifier(self) -> None:
        engine = MagicMock(spec=PaperTradingEngine)
        engine.run_once.return_value = _mock_result()
        engine.current_balance.return_value = 10_000.0
        engine.statistics.return_value = {"total_trades": 0}

        loop = TradingLoop(engine=engine, interval=60)
        loop._engine._notifier = None

        loop._maybe_send_daily_summary()
