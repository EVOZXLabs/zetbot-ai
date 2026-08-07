"""Reproduction tests for the watchdog heartbeat false-positive.

Symptom
-------
The bot stayed responsive (Telegram commands answered — the command center
runs in its own thread) yet the watchdog killed it because
``data/watchdog_heartbeat.json`` went stale.

Root cause
----------
The heartbeat was written by the main keep-alive loop only AFTER
``_monitor_positions()`` returned, on every ~60s monitor tick.  Any monitor
slowness, exception or hang delayed or completely stopped heartbeat writes,
so the watchdog read a stale mtime and restarted a perfectly healthy bot.

Fix
---
``main._start_heartbeat_writer`` runs the heartbeat on its own dedicated
daemon thread, independent of position monitoring, and the keep-alive loop
survives monitor exceptions (logged, not fatal).

All tests redirect ``data/`` into ``tmp_path`` via ``monkeypatch.chdir`` —
no real state files are touched.
"""

import json
import logging
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

import main as main_mod
from scripts.pipeline_scheduler import HEARTBEAT_FILE
from scripts.watchdog import Watchdog


def _logger() -> logging.Logger:
    return logging.getLogger("test.heartbeat.watchdog")


def _assert_heartbeat_fresh() -> None:
    assert os.path.exists(HEARTBEAT_FILE), "heartbeat file never written"
    age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    assert age < 1.0, f"heartbeat stale ({age:.2f}s) while bot is healthy"


# ---------------------------------------------------------------------------
#  Regression: heartbeat must not depend on _monitor_positions()
# ---------------------------------------------------------------------------


class TestHeartbeatIndependentOfMonitor:

    def test_heartbeat_keeps_updating_while_monitor_raises(
        self, tmp_path, monkeypatch,
    ) -> None:
        """_monitor_positions() raising every tick must not stop heartbeat
        updates (old code wrote the heartbeat only after the monitor
        returned, so an exception left it stale forever)."""
        monkeypatch.chdir(tmp_path)
        shutdown = threading.Event()
        thread = main_mod._start_heartbeat_writer(shutdown, _logger(), interval=0.02)

        def _monitor_positions_exception() -> None:
            raise RuntimeError("exchange ticker failed")

        # Simulate the keep-alive loop: monitoring raises every iteration.
        for _ in range(30):
            with pytest.raises(RuntimeError):
                _monitor_positions_exception()
            time.sleep(0.02)

        _assert_heartbeat_fresh()

        shutdown.set()
        thread.join(2.0)
        assert not thread.is_alive()

    def test_heartbeat_keeps_updating_while_monitor_hangs(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A monitor stuck on an exchange call must not stop heartbeat
        updates (old code wrote the heartbeat after the monitor, so a hang
        starved it and the watchdog killed a Telegram-responsive bot)."""
        monkeypatch.chdir(tmp_path)
        shutdown = threading.Event()
        thread = main_mod._start_heartbeat_writer(shutdown, _logger(), interval=0.02)

        release = threading.Event()

        def _monitor_positions_hangs() -> None:
            release.wait(5.0)

        stuck = threading.Thread(target=_monitor_positions_hangs)
        stuck.start()

        # The monitor is hung for the whole wait; the heartbeat must keep
        # refreshing regardless.
        time.sleep(0.4)
        _assert_heartbeat_fresh()

        release.set()
        stuck.join(2.0)
        assert not stuck.is_alive()

        shutdown.set()
        thread.join(2.0)
        assert not thread.is_alive()

    def test_startup_log_line(self, tmp_path, monkeypatch) -> None:
        """Startup must log '[WATCHDOG] heartbeat updated' once the first
        heartbeat has been written."""
        monkeypatch.chdir(tmp_path)
        shutdown = threading.Event()
        logs: list[str] = []

        class _FakeLogger:
            def info(self, msg: str, *args: object) -> None:
                logs.append(msg % args)

            def warning(self, msg: str, *args: object) -> None:
                logs.append(msg % args)

            def debug(self, msg: str, *args: object) -> None:
                logs.append(msg % args)

        thread = main_mod._start_heartbeat_writer(
            shutdown, _FakeLogger(), interval=60.0,
        )

        deadline = time.time() + 2.0
        while time.time() < deadline and not logs:
            time.sleep(0.01)

        assert "[WATCHDOG] heartbeat updated" in logs[0]
        _assert_heartbeat_fresh()

        shutdown.set()
        thread.join(2.0)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
#  Regression: an idle bot must keep the heartbeat fresh
# ---------------------------------------------------------------------------


class TestIdleBotHeartbeat:

    def test_heartbeat_keeps_updating_while_idle(
        self, tmp_path, monkeypatch,
    ) -> None:
        """An idle bot (no positions, no pipeline activity, no monitor
        ticks) must keep refreshing its heartbeat — the file timestamp must
        advance repeatedly.  Time is compressed: a 0.05s interval maps to
        the production ~30s cadence, so ~0.8s simulates several minutes."""
        monkeypatch.chdir(tmp_path)
        shutdown = threading.Event()
        thread = main_mod._start_heartbeat_writer(shutdown, _logger(), interval=0.05)

        seen_ts: set[str] = set()
        deadline = time.time() + 0.8
        while time.time() < deadline:
            if os.path.exists(HEARTBEAT_FILE):
                try:
                    with open(HEARTBEAT_FILE) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    # Polled mid-write (the writer uses a plain "w" open);
                    # the watchdog only reads mtime so this is harmless.
                    time.sleep(0.01)
                    continue
                seen_ts.add(data.get("ts", ""))
            time.sleep(0.01)

        shutdown.set()
        thread.join(2.0)
        assert not thread.is_alive()

        assert len(seen_ts) >= 5, (
            f"heartbeat only written {len(seen_ts)} time(s) while idle — "
            "expected continuous periodic updates"
        )
        _assert_heartbeat_fresh()


# ---------------------------------------------------------------------------
#  Regression: the watchdog must not restart a responsive bot
# ---------------------------------------------------------------------------


class TestWatchdogLeavesResponsiveBotAlone:

    def test_fresh_heartbeat_no_restart(self, tmp_path) -> None:
        """Bot alive with a fresh heartbeat → left running, never killed."""
        wd = Watchdog(project_root=str(tmp_path), interval=5.0)
        wd._bot_running = lambda: True
        wd._heartbeat_age = lambda: 10.0  # fresh
        wd._pipeline_age = lambda: None
        wd._notify = MagicMock()
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()

        assert wd.check_and_act() == "running"
        wd._kill_bot.assert_not_called()
        wd._do_restart.assert_not_called()

    def test_stale_then_refreshed_heartbeat_no_restart(self, tmp_path) -> None:
        """The exact false-positive scenario: the heartbeat looked stale on
        one check but the bot refreshed it before the watchdog re-checked —
        the watchdog must NOT kill a bot that is still writing heartbeats."""
        ages = iter([999.0, 5.0])  # stale at first check, fresh after grace
        wd = Watchdog(project_root=str(tmp_path), interval=5.0)
        wd._bot_running = lambda: True
        wd._heartbeat_age = lambda: next(ages)
        wd._pipeline_age = lambda: None
        wd._sleep = lambda seconds: False  # do not actually block the interval
        wd._notify = MagicMock()
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()

        assert wd.check_and_act() == "running"
        wd._kill_bot.assert_not_called()
        wd._do_restart.assert_not_called()
