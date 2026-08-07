"""Runtime-reliability audit reproductions (now asserting FIXED behavior).

Each test in this file pins the corrected behavior of a defect found
during the post-hardening runtime audit. They double as regression tests:
they fail if a fix regresses.

Repro 1 — BUY_OPENED notification must be retried when the send fails:
    ``main._notify_existing_positions`` only records a symbol in
    ``data/.notified_buys`` when the notification actually succeeded, so a
    transient Telegram failure at startup is retried on the next restart.

Repro 2 — BUY_OPENED must not duplicate after restart:
    ``paper_trading_engine._notify_buy`` records the symbol in
    ``data/.notified_buys`` (like ``scripts/pipeline.py`` run_execution),
    so the next restart's ``_notify_existing_positions`` does NOT send a
    second BUY_OPENED for the same open position.

Repro 3 — stale heartbeat file (reboot / device suspend) must not kill a
    bot that resumes, and heartbeat-stale restarts must not feed the
    watchdog crash rate limit (which previously caused a false HALT).

Repro 4 — the paper-engine startup run uses a daemon thread, so it can
    never block interpreter shutdown even when it outlives the 10s timeout.

Repro 5 — two concurrent ``data/.notified_buys`` writers (startup daemon
    thread + pipeline scheduler) must not clobber each other's entry: every
    BUY_OPENED dedup write routes through
    ``scripts.paper_state_lock.add_notified_buy`` (serialized + atomic).
"""
import json
import os
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest


def _open_position(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "entry_price": 100.0,
        "quantity": 1.0,
        "position_size_usdt": 100.0,
        "stop_loss": 95.0,
        "tp1": 105.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "status": "OPEN",
    }


# ============================================================================
#  Repro 1 — notified_buys dedup marks symbol even when send FAILS
# ============================================================================

class TestNotifiedBuysMarkedOnFailure:
    def test_failed_send_is_not_recorded_as_notified(self, tmp_path: Any, monkeypatch: Any) -> None:
        from main import _notify_existing_positions

        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [_open_position("BTC/USDT")]}, f)

        notifier = MagicMock()
        notifier.notify_buy_opened.return_value = False  # send failed

        _notify_existing_positions(MagicMock(), notifier)

        # FIXED: a failed delivery is NOT recorded as notified, so the
        # BUY_OPENED is retried on the next restart instead of being lost.
        assert not os.path.exists("data/.notified_buys")

    def test_failed_notification_is_retried_on_next_restart(self, tmp_path: Any, monkeypatch: Any) -> None:
        from main import _notify_existing_positions

        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [_open_position("BTC/USDT")]}, f)

        failing = MagicMock()
        failing.notify_buy_opened.return_value = False
        _notify_existing_positions(MagicMock(), failing)

        working = MagicMock()
        working.notify_buy_opened.return_value = True
        _notify_existing_positions(MagicMock(), working)

        # FIXED: the second (working) restart retries the pending symbol.
        working.notify_buy_opened.assert_called_once()

        # And once delivered, the symbol is recorded (dedup for later runs).
        with open("data/.notified_buys") as f:
            notified = {line.strip() for line in f if line.strip()}
        assert "BTC/USDT" in notified


# ============================================================================
#  Repro 2 — engine _notify_buy never records .notified_buys -> duplicate
# ============================================================================

class TestEngineNotifyBuyNotDeduped:
    def test_engine_buy_notification_is_deduped_across_restart(self, tmp_path: Any, monkeypatch: Any) -> None:
        from main import _notify_existing_positions
        from scripts.paper_trading_engine import PaperTradingEngine

        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        notifier = MagicMock()
        notifier.notify_buy_opened.return_value = True

        engine = PaperTradingEngine(notifier=notifier)
        plan = {
            "symbol": "BTC/USDT",
            "entry_price": 100.0,
            "quantity": 1.0,
            "position_size_usdt": 100.0,
            "stop_loss": 95.0,
            "tp1": 105.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "reasons": ["Paper trade executed"],
        }
        # Runtime BUY via the engine notification path (startup paper engine /
        # non-DI pipeline stage). AGENTS.md: _execute_plan() -> _notify_buy().
        engine._notify_buy(plan, 100.0, "order-1")
        assert notifier.notify_buy_opened.call_count == 1

        # FIXED: the engine path records data/.notified_buys (same format as
        # scripts/pipeline.py), so restart recovery skips this symbol.
        with open("data/.notified_buys") as f:
            notified = {line.strip() for line in f if line.strip()}
        assert "BTC/USDT" in notified

        # Restart recovery re-runs for the same still-open position.
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [_open_position("BTC/USDT")]}, f)
        _notify_existing_positions(MagicMock(), notifier)

        # FIXED: no second BUY_OPENED for the same position -> no duplicate.
        assert notifier.notify_buy_opened.call_count == 1

    def test_engine_failed_send_not_deduped(self, tmp_path: Any, monkeypatch: Any) -> None:
        """A failed engine notification is NOT recorded, so restart recovery
        retries it."""
        from main import _notify_existing_positions
        from scripts.paper_trading_engine import PaperTradingEngine

        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        notifier = MagicMock()
        notifier.notify_buy_opened.return_value = False

        engine = PaperTradingEngine(notifier=notifier)
        plan = {
            "symbol": "BTC/USDT",
            "entry_price": 100.0,
            "quantity": 1.0,
            "position_size_usdt": 100.0,
            "stop_loss": 95.0,
            "tp1": 105.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "reasons": ["Paper trade executed"],
        }
        engine._notify_buy(plan, 100.0, "order-1")
        assert notifier.notify_buy_opened.call_count == 1

        # FIXED: failed delivery is not recorded.
        assert not os.path.exists("data/.notified_buys")

        # Restart recovery re-sends it.
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [_open_position("BTC/USDT")]}, f)
        _notify_existing_positions(MagicMock(), notifier)
        assert notifier.notify_buy_opened.call_count == 2


# ============================================================================
#  Repro 3 — stale heartbeat file (reboot/suspend) must NOT kill a bot that
#  resumes, and heartbeat-stale restarts must NOT feed the crash rate limit
# ============================================================================

class TestWatchdogStaleHeartbeatKillLoop:
    def _make_wd(self, tmp_path: Any):
        from scripts.watchdog import Watchdog
        return Watchdog(project_root=str(tmp_path), interval=20.0)

    def test_stale_heartbeat_refreshed_after_interval_not_killed(self, tmp_path: Any) -> None:
        """FIXED: a bot that merely resumed from device suspend refreshes its
        heartbeat; the one-interval re-check sees the fresh heartbeat and the
        watchdog does NOT kill it."""
        from scripts.watchdog import HEARTBEAT_STALE_SECONDS

        wd = self._make_wd(tmp_path)
        # First check: stale (as after reboot/suspend). After the grace
        # interval: refreshed (the healthy bot wrote its heartbeat).
        ages = iter([HEARTBEAT_STALE_SECONDS + 60, 30.0])
        wd._heartbeat_age = lambda: next(ages)
        wd._bot_running = lambda: True
        wd._pipeline_age = lambda: None
        wd._sleep = lambda _secs: False
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()
        wd._notify = MagicMock()

        action = wd.check_and_act()

        assert action == "running"
        wd._kill_bot.assert_not_called()
        wd._do_restart.assert_not_called()

    def test_stale_heartbeat_still_killed_when_never_refreshed(self, tmp_path: Any) -> None:
        """A genuinely hung bot stays stale after the grace interval and IS
        killed — but the restart is NOT registered as a crash."""
        from scripts.watchdog import HEARTBEAT_STALE_SECONDS

        wd = self._make_wd(tmp_path)
        wd._heartbeat_age = lambda: HEARTBEAT_STALE_SECONDS + 60  # always stale
        wd._bot_running = lambda: True
        wd._pipeline_age = lambda: None
        wd._sleep = lambda _secs: False
        wd._crash_rate_exceeded = lambda: False
        wd._register_crash = MagicMock()
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()
        wd._notify = MagicMock()

        action = wd.check_and_act()

        assert action == "heartbeat_stale"
        wd._kill_bot.assert_called_once()
        wd._do_restart.assert_called_once()
        # FIXED: a heartbeat-stale restart is not a crash.
        wd._register_crash.assert_not_called()
        # First stale occurrence -> streak counter starts, well under the
        # dedicated halt threshold.
        assert wd._heartbeat_stale_streak == 1

    def test_sleep_wake_cycles_never_accumulate_halt(self, tmp_path: Any) -> None:
        """Sleep/wake blips: the heartbeat is stale at the first check but
        refreshed by the re-check (the woken bot writes it ~60s after
        resume). Repeated wake-ups must NOT accumulate the stale streak,
        feed the crash rate limit, or halt — only a genuinely hung bot may.
        """
        import itertools

        from scripts.watchdog import HEARTBEAT_STALE_SECONDS

        wd = self._make_wd(tmp_path)
        ages = itertools.cycle([HEARTBEAT_STALE_SECONDS + 60, 30.0])
        wd._heartbeat_age = lambda: next(ages)
        wd._bot_running = lambda: True
        wd._pipeline_age = lambda: None
        wd._sleep = lambda _secs: False
        wd._register_crash = MagicMock()
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()
        wd._notify = MagicMock()

        for _ in range(8):
            action = wd.check_and_act()
            assert action == "running"

        wd._kill_bot.assert_not_called()
        wd._do_restart.assert_not_called()
        wd._register_crash.assert_not_called()
        assert wd._heartbeat_stale_streak == 0

    def test_hung_bot_stale_across_many_cycles_halts_not_loops(self, tmp_path: Any) -> None:
        """A genuinely hung bot (heartbeat NEVER refreshes) must not restart
        forever now that stale restarts are excluded from _register_crash():
        the dedicated consecutive-stale streak HALTS auto-restart after
        MAX_HEARTBEAT_STALE_RESTARTS stale observations."""
        from scripts.watchdog import (
            HEARTBEAT_STALE_SECONDS,
            MAX_HEARTBEAT_STALE_RESTARTS,
        )

        wd = self._make_wd(tmp_path)
        wd._heartbeat_age = lambda: HEARTBEAT_STALE_SECONDS + 60  # never refreshes
        wd._bot_running = lambda: True
        wd._pipeline_age = lambda: None
        wd._sleep = lambda _secs: False
        wd._register_crash = MagicMock()
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()
        wd._notify = MagicMock()

        actions: list[str] = []
        for _ in range(10):
            actions.append(wd.check_and_act())
            if actions[-1] == "halted":
                break

        # Stopping condition reached — not an infinite restart loop.
        assert actions[-1] == "halted"
        # MAX-1 restart attempts, then the halt on the MAX-th stale check.
        assert actions.count("heartbeat_stale") == MAX_HEARTBEAT_STALE_RESTARTS - 1
        assert wd._kill_bot.call_count == MAX_HEARTBEAT_STALE_RESTARTS - 1
        assert wd._do_restart.call_count == MAX_HEARTBEAT_STALE_RESTARTS - 1
        # Stale restarts still never fed the crash rate limit.
        wd._register_crash.assert_not_called()

        # The halt is sticky: the watchdog stands by, it does not keep killing.
        wd._kill_bot.reset_mock()
        assert wd.check_and_act() == "halted"
        wd._kill_bot.assert_not_called()


# ============================================================================
#  Repro 5 — concurrent .notified_buys writers must not lose a symbol
# ============================================================================

class TestNotifiedBuysConcurrentWrites:
    def test_concurrent_writers_keep_both_symbols(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Paper-engine startup (daemon thread, ``main._notify_existing_positions``)
        and the pipeline scheduler both record BUY_OPENED symbols in
        ``data/.notified_buys``. Each does a read-modify-write; without the
        ``PAPER_STATE_LOCK`` helper one thread's stale read can clobber the
        other thread's entry (BUG-4). FIXED: the engine, pipeline and
        startup paths all route through ``add_notified_buy``.
        """
        from scripts.paper_state_lock import add_notified_buy

        monkeypatch.chdir(tmp_path)
        os.makedirs("data", exist_ok=True)

        barrier = threading.Barrier(2)

        def record(sym: str) -> None:
            barrier.wait(timeout=10)
            add_notified_buy(sym)

        t1 = threading.Thread(target=record, args=("BTC/USDT",))
        t2 = threading.Thread(target=record, args=("ETH/USDT",))
        t1.start()
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)

        assert not t1.is_alive() and not t2.is_alive()

        with open("data/.notified_buys") as f:
            notified = {line.strip() for line in f if line.strip()}
        # FIXED: neither writer's symbol is lost.
        assert "BTC/USDT" in notified
        assert "ETH/USDT" in notified


# ============================================================================
#  Repro 4 — paper-engine startup runs in a daemon thread (no shutdown block)
# ============================================================================

class TestPaperStartupThreadLeak:
    def test_daemon_thread_cannot_block_shutdown(self) -> None:
        """Faithful reproduction of the FIXED main.py startup pattern:

            startup_thread = threading.Thread(
                target=_paper_startup_worker, name="PaperStartup", daemon=True,
            )
            startup_thread.start()
            startup_thread.join(timeout=10)

        The old ThreadPoolExecutor left a NON-daemon worker alive after the
        10s timeout, so the paper engine kept running concurrently with the
        monitor AND blocked interpreter shutdown. The daemon thread can still
        outlive the timeout (executing plans, reconciling TP/SL), but it can
        never block sys.exit(0).
        """
        done = threading.Event()

        def slow_worker() -> None:
            done.wait(30)

        startup_thread = threading.Thread(
            target=slow_worker,
            name="PaperStartup",
            daemon=True,
        )
        startup_thread.start()
        startup_thread.join(timeout=0.3)

        # FIXED: the join returns after the timeout while the worker keeps
        # running — but the worker is a daemon...
        assert startup_thread.is_alive()
        assert startup_thread.daemon is True
        # ...and, unlike the old ThreadPoolExecutor worker, it is never the
        # interpreter's sole non-daemon thread: daemon threads do not block
        # process exit, so shutdown cannot hang on it.
        alive_non_daemon = [
            t for t in threading.enumerate()
            if t.is_alive() and not t.daemon
        ]
        assert startup_thread not in alive_non_daemon

        done.set()
        startup_thread.join(timeout=5)
        assert not startup_thread.is_alive()
