"""Unit tests for the process watchdog (scripts/watchdog.py).

All tests run against a ``FakeWatchdog`` harness that overrides the process /
filesystem / clock hooks, so no real processes are spawned and no real state
files are touched. Only pure helpers and CLI read-only paths use the real
implementations (against ``tmp_path``).
"""

import json
import logging
import sys
import time
from unittest.mock import MagicMock

import pytest

from scripts.watchdog import (
    HALT_FILE,
    PAUSE_FILE,
    SHUTDOWN_FILE,
    Watchdog,
    _default_python,
    main,
    parse_args,
    print_status,
    stop_watchdog,
)


class FakeWatchdog(Watchdog):
    """Watchdog with all side-effecting hooks faked."""

    def __init__(self, project_root: str, **kwargs: object) -> None:
        kwargs.setdefault("logger", logging.getLogger("test.watchdog"))
        super().__init__(project_root, **kwargs)
        self.files: set[str] = set()
        self.clock: list[float] = [1_000_000.0]
        self.running = False
        self.rc: int | None = None
        self.spawn_calls: list[float] = []
        self.notifications: list[tuple[str, bool]] = []
        self.persisted_crashes: list[list[float]] = []

    def _file_exists(self, rel_path: str) -> bool:
        return rel_path in self.files

    def _remove_file(self, rel_path: str) -> None:
        self.files.discard(rel_path)

    def _now(self) -> float:
        return self.clock[0]

    def _sleep(self, seconds: float) -> bool:
        self.clock[0] += seconds
        return self._stop_event.is_set()

    def _bot_running(self) -> bool:
        if not self.running:
            self.last_returncode = self.rc
        return self.running

    def _spawn_bot(self) -> None:
        self.spawn_calls.append(self.clock[0])
        self.running = True
        self.rc = None
        self.last_returncode = None

    def _notify(self, text: str, error: bool = False) -> None:
        self.notifications.append((text, error))

    def _persist_crash_times(self) -> None:
        self.persisted_crashes.append(list(self._crash_times))

    def _load_crash_times(self) -> None:
        self._crash_times = []

    def _write_halt_file(self) -> None:
        self.files.add(HALT_FILE)


class DeathSpiralWatchdog(FakeWatchdog):
    """Bot comes back up and immediately dies again (crash loop)."""

    def _spawn_bot(self) -> None:
        super()._spawn_bot()
        self.running = False
        self.rc = 9


def _wd(tmp_path, **kwargs):
    return FakeWatchdog(str(tmp_path), **kwargs)


# ---------------------------------------------------------------------------
#  Decision logic
# ---------------------------------------------------------------------------

class TestCheckAndAct:

    def test_running_bot_is_left_alone(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = True
        assert wd.check_and_act() == "running"
        assert wd.spawn_calls == []

    def test_pause_flag_beats_running(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = True
        wd.files.add(PAUSE_FILE)
        assert wd.check_and_act() == "paused"
        assert wd.spawn_calls == []

    def test_paused_bot_never_restarts(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 9
        wd.files.add(PAUSE_FILE)
        assert wd.check_and_act() == "paused"
        assert wd.spawn_calls == []

    def test_graceful_exit_is_a_deliberate_stop(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 0
        assert wd.check_and_act() == "stopped_manual"
        assert wd.spawn_calls == []

    def test_shutdown_file_is_a_deliberate_stop(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 9
        wd.files.add(SHUTDOWN_FILE)
        assert wd.check_and_act() == "stopped_manual"
        assert wd.spawn_calls == []

    def test_manual_stop_notified_exactly_once(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 0
        for _ in range(3):
            assert wd.check_and_act() == "stopped_manual"
        assert len(wd.notifications) == 1
        assert "manual" in wd.notifications[0][0].lower()

    def test_crash_restarts_and_notifies(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 9
        assert wd.check_and_act() == "restarted"
        assert wd.spawn_calls == [wd.clock[0]]
        assert any("RESTARTED" in text for text, _ in wd.notifications)

    def test_restart_clears_stale_shutdown_file(self, tmp_path):
        wd = _wd(tmp_path)
        wd.files.add(SHUTDOWN_FILE)
        wd._do_restart()
        assert SHUTDOWN_FILE not in wd.files
        assert wd.spawn_calls

    def test_halt_file_is_sticky(self, tmp_path):
        wd = _wd(tmp_path)
        wd.running = False
        wd.rc = 9
        wd.files.add(HALT_FILE)
        assert wd.check_and_act() == "halted"
        assert wd.spawn_calls == []

    def test_halt_beats_pause_flag(self, tmp_path):
        wd = _wd(tmp_path)
        wd.files.add(HALT_FILE)
        wd.files.add(PAUSE_FILE)
        assert wd.check_and_act() == "halted"

    def test_ensure_bot_stands_by_when_halted(self, tmp_path):
        wd = _wd(tmp_path)
        wd.files.add(HALT_FILE)
        wd._ensure_bot()
        assert wd.spawn_calls == []

    def test_ensure_bot_starts_when_nothing_running(self, tmp_path):
        wd = _wd(tmp_path)
        wd._ensure_bot()
        assert wd.spawn_calls == [wd.clock[0]]


# ---------------------------------------------------------------------------
#  Crash history / rate limit
# ---------------------------------------------------------------------------

class TestRateLimit:

    def test_halt_after_max_restarts_in_window(self, tmp_path):
        wd = _wd(tmp_path, max_restarts=3, window=600.0)
        for _ in range(3):
            wd.running = False
            wd.rc = 9
            assert wd.check_and_act() == "restarted"
            wd.clock[0] += 60
        wd.running = False
        wd.rc = 9
        wd.clock[0] += 60
        assert wd.check_and_act() == "halted"
        assert HALT_FILE in wd.files
        assert any("MANUAL INTERVENTION" in text for text, _ in wd.notifications)

    def test_restart_count_is_persisted(self, tmp_path):
        wd = _wd(tmp_path, max_restarts=10, window=600.0)
        wd.running = False
        wd.rc = 9
        wd.check_and_act()
        assert wd.persisted_crashes and len(wd.persisted_crashes[-1]) == 1

    def test_crashes_expire_after_window(self, tmp_path):
        wd = _wd(tmp_path, max_restarts=3, window=600.0)
        wd._crash_times = [
            wd.clock[0] - 601, wd.clock[0] - 700,
            wd.clock[0] - 800, wd.clock[0] - 900,
        ]
        wd.running = False
        wd.rc = 9
        assert wd.check_and_act() == "restarted"

    def test_old_crashes_within_window_count(self, tmp_path):
        wd = _wd(tmp_path, max_restarts=3, window=600.0)
        wd._crash_times = [
            wd.clock[0] - 10, wd.clock[0] - 20,
            wd.clock[0] - 30, wd.clock[0] - 40,
        ]
        wd.running = False
        wd.rc = 9
        assert wd.check_and_act() == "halted"

    def test_run_exits_nonzero_on_halt(self, tmp_path):
        wd = DeathSpiralWatchdog(
            str(tmp_path), interval=20.0, max_restarts=1, window=600.0,
        )
        rc = wd.run()
        assert rc == 1
        assert HALT_FILE in wd.files
        assert len(wd.spawn_calls) == 2

    def test_run_returns_zero_when_stop_requested(self, tmp_path):
        wd = _wd(tmp_path, interval=20.0)
        wd.running = True
        wd._stop_event.set()
        assert wd.run() == 0


# ---------------------------------------------------------------------------
#  Crash timestamps round-trip (real persistence, tmp filesystem)
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_crash_times_roundtrip(self, tmp_path):
        root = str(tmp_path)
        now = time.time()
        a = Watchdog(root, logger=logging.getLogger("test.persist.a"))
        a._crash_times = [now - 10, now - 20]
        a._persist_crash_times()
        b = Watchdog(root, logger=logging.getLogger("test.persist.b"))
        b._load_crash_times()
        assert b._crash_times == [now - 10, now - 20]

    def test_halt_file_contains_crash_list(self, tmp_path):
        wd = Watchdog(str(tmp_path), logger=logging.getLogger("test.persist.halt"))
        wd._crash_times = [100.0, 200.0]
        wd._write_halt_file()
        data = json.loads((tmp_path / HALT_FILE).read_text())
        assert data["crashes"] == [100.0, 200.0]
        assert "halted_at" in data


# ---------------------------------------------------------------------------
#  Helpers / CLI
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_default_python_prefers_project_venv(self, tmp_path):
        venv = tmp_path / ".venv" / "bin" / "python"
        venv.parent.mkdir(parents=True)
        venv.touch()
        assert _default_python(str(tmp_path)) == str(venv)

    def test_default_python_falls_back_to_sys_executable(self, tmp_path):
        assert _default_python(str(tmp_path)) == sys.executable

    def test_rc_text(self):
        assert Watchdog._rc_text(None) == "process no longer running (attached instance)"
        assert Watchdog._rc_text(0) == "exit code 0 (graceful stop)"
        assert Watchdog._rc_text(-9) == "killed by signal 9 (SIGTERM=-15, SIGKILL=-9/OOM)"
        assert Watchdog._rc_text(7) == "exit code 7"

    def test_real_notify_routes_to_error_and_system(self):
        wd = Watchdog(
            "/tmp/x",
            logger=logging.getLogger("test.notify"),
            notifier=MagicMock(),
        )
        wd._notify("boom", error=True)
        wd._notify("info")
        wd._notifier.notify_error.assert_called_once_with("boom")
        wd._notifier.notify_system.assert_called_once_with("info")

    def test_notify_failure_is_swallowed(self):
        notifier = MagicMock()
        notifier.notify_system.side_effect = RuntimeError("telegram down")
        wd = Watchdog(
            "/tmp/x",
            logger=logging.getLogger("test.notify.fail"),
            notifier=notifier,
        )
        wd._notify("should not raise")


class TestCli:

    def test_parse_args_env_defaults(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_INTERVAL", "30")
        monkeypatch.setenv("WATCHDOG_MAX_RESTARTS", "5")
        monkeypatch.setenv("WATCHDOG_WINDOW", "900")
        monkeypatch.setenv("WATCHDOG_PYTHON", "/usr/bin/python3")
        args = parse_args([])
        assert args.interval == 30.0
        assert args.max_restarts == 5
        assert args.window == 900.0
        assert args.python == "/usr/bin/python3"

    def test_print_status_is_readonly(self, tmp_path, capsys):
        print_status(str(tmp_path))
        out = capsys.readouterr().out
        assert "project root" in out
        assert "bot" in out

    def test_stop_without_pid_file_returns_error(self, tmp_path):
        assert stop_watchdog(str(tmp_path)) == 1

    def test_main_status(self, tmp_path):
        assert main(["--project-root", str(tmp_path), "--status"]) == 0

    def test_main_stop_without_watchdog(self, tmp_path):
        assert main(["--project-root", str(tmp_path), "--stop"]) == 1
