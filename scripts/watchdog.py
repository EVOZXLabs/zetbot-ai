#!/usr/bin/env python3
"""ZetBot AI — Watchdog / auto-restart supervisor.

Indodax has NO native stop orders: SL/TP are only executed by the bot's own
reconciliation loop, so a dead bot == an unprotected live position. This
watchdog keeps the bot alive and restarts it as fast as possible (default
check interval 20s, far below the 5-minute pipeline cycle).

Behaviour
---------
* Supervises the bot as a child process (recommended). If the bot was
  already started manually (e.g. ``make run`` in tmux) it ATTACHES to the
  running instance via ``data/zetbot.pid`` instead of starting a second one.
* On crash, restarts the bot with the exact command used for a manual start
  (``.venv/bin/python main.py`` from the project root).
* Does NOT auto-restart when the stop was deliberate:
    - ``data/.shutdown_requested``  -> user asked for a permanent stop,
    - ``data/.watchdog_paused``     -> user paused auto-restart,
    - graceful exit (exit code 0)   -> the bot was stopped cleanly.
  In every other case a stale ``.shutdown_requested`` is cleared BEFORE the
  restart so the freshly started bot does not immediately shut down again.
* Rate-limits restart loops: more than ``--max-restarts`` crashes inside
  ``--window`` seconds halts auto-restart, writes ``.watchdog_halt`` and
  alerts "manual intervention needed" (crash timestamps persist across
  watchdog restarts). The halt is STICKY: the watchdog exits non-zero and
  every restart keeps standing by until the operator removes
  ``data/.watchdog_halt``.
* Notifies via the existing Telegram notifier (``bot/notifier.py``), which
  reads ``TELEGRAM_*`` from ``.env``.

Run (foreground — under systemd, tmux, screen or termux-services)::

    .venv/bin/python scripts/watchdog.py

State files (all under ``data/``):
    zetbot.pid             bot's own PID lock file (written by main.py)
    zetbot-watchdog.pid    this watchdog's PID (used by ``--stop``)
    .shutdown_requested    bot graceful-stop signal (respected; cleared only
                           right before an auto-restart of a crashed bot)
    .watchdog_paused       user pauses auto-restart (never restarts)
    .watchdog_halt         set when the rate-limit halts auto-restart
    .watchdog_crashes.json persisted crash timestamps (rate-limit window)

Exit codes: 0 on normal ``--stop``/signal, non-zero on rate-limit halt so a
supervisor (systemd ``Restart=always``) knows intervention was reached.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
#  Paths — computed from this file's location, and made importable so the
#  notifier (bot.notifier) can be imported lazily below.
# ---------------------------------------------------------------------------

PROJECT_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Relative paths (under the project root / data dir)
SHUTDOWN_FILE = "data/.shutdown_requested"
PAUSE_FILE = "data/.watchdog_paused"
HALT_FILE = "data/.watchdog_halt"
CRASH_FILE = "data/.watchdog_crashes.json"
BOT_PID_FILE = "data/zetbot.pid"
WATCHDOG_PID_FILE = "data/zetbot-watchdog.pid"


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_python(project_root: str) -> str:
    """Prefer the project's own venv python (same one ``make run`` uses)."""
    venv = os.path.join(project_root, ".venv", "bin", "python")
    if os.path.exists(venv):
        return venv
    return sys.executable


def _make_logger(project_root: str) -> logging.Logger:
    logger = logging.getLogger("zetbot.watchdog")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    try:
        log_dir = os.path.join(project_root, "logs")
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(os.path.join(log_dir, "watchdog.log"))
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass
    logger.propagate = False
    return logger


def _make_notifier(project_root: str) -> Optional[Any]:
    """Build the shared Telegram notifier from ``.env`` (or None)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(project_root, ".env"))
    except Exception:
        pass
    try:
        from bot.notifier import Notifier
        return Notifier.from_env()
    except Exception:
        return None


class Watchdog:
    """Process-liveness supervisor for the ZetBot AI trading bot.

    The decision logic lives in ``check_and_act()`` and is deliberately
    driven through small hook methods (``_bot_running``, ``_file_exists``,
    ``_spawn_bot``, ``_now``, ``_notify`` ...) so tests can exercise it
    without spawning real processes or touching the real filesystem.
    """

    def __init__(
        self,
        project_root: str,
        *,
        interval: float = 20.0,
        max_restarts: int = 3,
        window: float = 600.0,
        python_cmd: str = "",
        logger: Optional[logging.Logger] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self.project_root = project_root
        self.interval = float(interval)
        self.max_restarts = int(max_restarts)
        self.window = float(window)
        self.python_cmd = python_cmd or _default_python(project_root)
        self.logger = logger or _make_logger(project_root)
        self._notifier = notifier
        self.child: Optional[subprocess.Popen] = None
        self.last_returncode: Optional[int] = None
        self._crash_times: list[float] = []
        self._manual_stop_notified: bool = False
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    #  Hooks (overridden by tests / subclasses)
    # ------------------------------------------------------------------

    def _file_exists(self, rel_path: str) -> bool:
        return os.path.exists(os.path.join(self.project_root, rel_path))

    def _remove_file(self, rel_path: str) -> None:
        try:
            os.remove(os.path.join(self.project_root, rel_path))
        except OSError:
            pass

    def _now(self) -> float:
        return time.time()

    def _sleep(self, seconds: float) -> bool:
        """Wait up to ``seconds``; returns True when a stop was requested."""
        return self._stop_event.wait(seconds)

    def _child_rc(self) -> Optional[int]:
        if self.child is None:
            return None
        return self.child.poll()

    def _bot_running(self) -> bool:
        """True when the supervised child is alive, or an external bot
        instance is found via the PID file."""
        if self.child is not None:
            rc = self.child.poll()
            if rc is None:
                return True
            self.last_returncode = rc
            return False
        return self._external_bot_pid() is not None

    def _external_bot_pid(self) -> Optional[int]:
        pid = self._read_pid_file(os.path.join(self.project_root, BOT_PID_FILE))
        if pid is None:
            return None
        if not self._pid_alive(pid) or not self._pid_is_bot(pid):
            return None
        return pid

    @staticmethod
    def _read_pid_file(path: str) -> Optional[int]:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _pid_is_bot(pid: int) -> bool:
        """Guard against a recycled PID: only treat the process as the bot
        when its command line actually runs ``main.py``."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            return "main.py" in cmd
        except OSError:
            # No /proc (rare, e.g. some containers) — trust the PID file.
            return True

    def _spawn_bot(self) -> None:
        log_path = os.path.join(self.project_root, "logs", "bot-console.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        cmd = [self.python_cmd, "main.py"]
        self.logger.info("spawning bot: %s (cwd=%s)", " ".join(cmd), self.project_root)
        with open(log_path, "ab") as out:
            self.child = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
        self.last_returncode = None

    def _notify(self, text: str, error: bool = False) -> None:
        try:
            notifier = self._get_notifier()
            if notifier is None:
                return
            if error:
                notifier.notify_error(text)
            else:
                notifier.notify_system(text)
        except Exception:
            self.logger.warning("watchdog notification failed", exc_info=True)

    def _get_notifier(self) -> Optional[Any]:
        if self._notifier is None:
            self._notifier = _make_notifier(self.project_root)
        return self._notifier

    def _graceful_stop(self) -> bool:
        """A clean exit (code 0) means the operator stopped the bot
        deliberately — do not auto-restart."""
        return self.last_returncode == 0

    # ------------------------------------------------------------------
    #  Crash history / rate limit
    # ------------------------------------------------------------------

    def _load_crash_times(self) -> None:
        path = os.path.join(self.project_root, CRASH_FILE)
        try:
            with open(path) as f:
                data = json.load(f)
            times = [float(t) for t in data.get("crashes", [])]
        except Exception:
            times = []
        now = self._now()
        self._crash_times = [t for t in times if now - t <= self.window]

    def _persist_crash_times(self) -> None:
        try:
            os.makedirs(os.path.join(self.project_root, "data"), exist_ok=True)
            with open(os.path.join(self.project_root, CRASH_FILE), "w") as f:
                json.dump({"crashes": self._crash_times}, f)
        except OSError:
            pass

    def _register_crash(self) -> None:
        now = self._now()
        self._crash_times = [t for t in self._crash_times if now - t <= self.window]
        self._crash_times.append(now)
        self._persist_crash_times()

    def _recent_crashes(self) -> list[float]:
        now = self._now()
        return [t for t in self._crash_times if now - t <= self.window]

    def _crash_rate_exceeded(self) -> bool:
        return len(self._recent_crashes()) > self.max_restarts

    # ------------------------------------------------------------------
    #  Decision logic
    # ------------------------------------------------------------------

    def check_and_act(self) -> str:
        """One supervision iteration. Returns the action taken:
        ``"paused"``, ``"running"``, ``"stopped_manual"``, ``"halted"`` or
        ``"restarted"``."""
        if self._file_exists(HALT_FILE):
            self.logger.warning(
                "auto-restart halted (%s) — waiting for manual intervention",
                HALT_FILE,
            )
            return "halted"

        if self._file_exists(PAUSE_FILE):
            self.logger.debug("watchdog paused (%s) — no action", PAUSE_FILE)
            return "paused"

        if self._bot_running():
            self._manual_stop_notified = False
            return "running"

        # Bot is down. Was the stop deliberate?
        if self._file_exists(SHUTDOWN_FILE) or self._graceful_stop():
            if not self._manual_stop_notified:
                self._notify(
                    "🛑 *WATCHDOG* — bot stopped (manual/graceful)\n\n"
                    "No auto-restart: the stop was deliberate "
                    "(shutdown file or clean exit).\n"
                    "Start the bot again manually, or restart the watchdog "
                    "to resume supervision.",
                )
                self._manual_stop_notified = True
            self.logger.info(
                "bot stopped deliberately (%s) — not restarting",
                "shutdown file" if self._file_exists(SHUTDOWN_FILE) else "graceful exit",
            )
            return "stopped_manual"

        # Crash -> apply the rate limit, then restart.
        self._register_crash()
        if self._crash_rate_exceeded():
            self._halt()
            return "halted"

        self._do_restart()
        return "restarted"

    def _do_restart(self) -> None:
        # A stale shutdown signal from an earlier run would make the freshly
        # started bot exit again — clear it before restarting. (A shutdown
        # file is only respected as "deliberate stop" while the bot is down;
        # by reaching this branch we already know the stop was NOT deliberate.)
        self._remove_file(SHUTDOWN_FILE)
        reason = self._rc_text(self.last_returncode)
        self._spawn_bot()
        self._notify(
            f"🔁 *WATCHDOG* — BOT RESTARTED (auto)\n\n"
            f"Reason: {reason}\n"
            f"Time: {_ts()}\n"
            f"Crashes in last {int(self.window / 60)} min: {len(self._recent_crashes())}",
        )
        self.logger.warning("bot down (%s) — restarting", reason)

    def _halt(self) -> None:
        count = len(self._recent_crashes())
        minutes = int(self.window / 60)
        self._notify(
            f"🚨 *WATCHDOG* — MANUAL INTERVENTION NEEDED\n\n"
            f"The bot has restarted more than {self.max_restarts} times in "
            f"the last {minutes} min ({count} crashes) and keeps dying.\n"
            f"Auto-restart has been HALTED to avoid a crash loop.\n\n"
            f"Fix the bug, then restart the watchdog:\n"
            f"  `{self.python_cmd} scripts/watchdog.py`\n\n"
            f"⚠️ While the bot is down there is NO SL/TP protection on "
            f"exchanges without native stop orders (e.g. indodax).",
            error=True,
        )
        self.logger.error(
            "rate limit exceeded (%d crashes in %ds) — halting auto-restart",
            count, self.window,
        )
        self._write_halt_file()

    def _write_halt_file(self) -> None:
        try:
            os.makedirs(os.path.join(self.project_root, "data"), exist_ok=True)
            with open(os.path.join(self.project_root, HALT_FILE), "w") as f:
                json.dump(
                    {"halted_at": _ts(), "crashes": self._crash_times},
                    f,
                )
        except OSError:
            pass

    @staticmethod
    def _rc_text(rc: Optional[int]) -> str:
        if rc is None:
            return "process no longer running (attached instance)"
        if rc == 0:
            return "exit code 0 (graceful stop)"
        if rc < 0:
            return f"killed by signal {-rc} (SIGTERM=-15, SIGKILL=-9/OOM)"
        return f"exit code {rc}"

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.logger.info(
            "signal %s received — watchdog exiting (the bot child keeps "
            "running unless you stop it separately)", signum,
        )
        self._stop_event.set()

    def _write_watchdog_pid(self) -> None:
        try:
            os.makedirs(os.path.join(self.project_root, "data"), exist_ok=True)
            with open(os.path.join(self.project_root, WATCHDOG_PID_FILE), "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def _ensure_bot(self) -> None:
        """Start (or attach to) the bot at watchdog startup."""
        if self.child is not None and self._child_rc() is None:
            return
        if self._file_exists(HALT_FILE):
            self.logger.warning(
                "auto-restart is halted (%s) — standing by", HALT_FILE,
            )
            self._manual_stop_notified = False
            return
        if self._file_exists(SHUTDOWN_FILE) or self._file_exists(PAUSE_FILE):
            self.logger.info(
                "%s present — standing by (bot not started)",
                SHUTDOWN_FILE if self._file_exists(SHUTDOWN_FILE) else PAUSE_FILE,
            )
            self._manual_stop_notified = False
            return
        ext = self._external_bot_pid()
        if ext is not None:
            self.logger.info("attaching to already-running bot (pid %s)", ext)
            return
        self.logger.info("no bot running — starting it now")
        self._spawn_bot()

    def _notify_started(self) -> None:
        if self._file_exists(HALT_FILE):
            self._notify(
                "🔴 *WATCHDOG* — restarted, but auto-restart remains HALTED\n\n"
                "A crash loop was detected earlier and auto-restart is still "
                "disabled. Fix the bug, then remove `data/.watchdog_halt` to "
                "re-arm the watchdog.\n\n"
                "⚠️ While the bot is down there is NO SL/TP protection on "
                "exchanges without native stop orders (e.g. indodax).",
                error=True,
            )
            return
        self._notify(
            f"🟢 *WATCHDOG* — armed\n\n"
            f"Supervising the trading bot every {int(self.interval)}s.\n"
            f"Auto-restart on crash is ON — unless paused "
            f"(`data/.watchdog_paused`) or stopped manually.",
        )

    def run(self) -> int:
        self._load_crash_times()
        self._write_watchdog_pid()
        self._install_signal_handlers()
        self._ensure_bot()
        self._notify_started()
        self.logger.info(
            "watchdog started — interval=%ss max_restarts=%d window=%ss",
            self.interval, self.max_restarts, self.window,
        )
        while True:
            if self._sleep(self.interval):
                break
            try:
                action = self.check_and_act()
                if action != "running":
                    self.logger.info("watchdog action: %s", action)
                if action == "halted":
                    self.logger.error(
                        "auto-restart halted — exiting watchdog "
                        "(supervisor may retry; halt file is sticky)",
                    )
                    return 1
            except Exception:
                self.logger.exception("watchdog iteration failed")
        return 0


# ---------------------------------------------------------------------------
#  CLI helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: Optional[int]) -> bool:
    return pid is not None and Watchdog._pid_alive(pid)


def print_status(project_root: str) -> None:
    bot_pid = Watchdog._read_pid_file(os.path.join(project_root, BOT_PID_FILE))
    wd_pid = Watchdog._read_pid_file(os.path.join(project_root, WATCHDOG_PID_FILE))
    print(f"project root : {project_root}")
    print(f"bot          : pid={bot_pid} alive={_pid_alive(bot_pid)}")
    print(f"watchdog     : pid={wd_pid} alive={_pid_alive(wd_pid)}")
    for flag in (SHUTDOWN_FILE, PAUSE_FILE, HALT_FILE):
        state = "present" if os.path.exists(os.path.join(project_root, flag)) else "absent"
        print(f"flag         : {flag} -> {state}")


def stop_watchdog(project_root: str) -> int:
    pid = Watchdog._read_pid_file(os.path.join(project_root, WATCHDOG_PID_FILE))
    if pid is None:
        print("no watchdog PID file found — nothing to stop")
        return 1
    if not _pid_alive(pid):
        print(f"watchdog pid {pid} is not running (stale PID file)")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        print(f"permission denied signalling watchdog pid {pid}")
        return 1
    print(f"watchdog (pid {pid}) stopped — the bot keeps running unless stopped separately")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="watchdog",
        description="ZetBot AI watchdog — auto-restart supervisor for the trading bot.",
    )
    parser.add_argument(
        "--interval", type=float,
        default=float(os.getenv("WATCHDOG_INTERVAL", "20")),
        help="supervision interval in seconds (default 20)",
    )
    parser.add_argument(
        "--max-restarts", type=int,
        default=int(os.getenv("WATCHDOG_MAX_RESTARTS", "3")),
        help="max restarts inside --window before auto-restart halts (default 3)",
    )
    parser.add_argument(
        "--window", type=float,
        default=float(os.getenv("WATCHDOG_WINDOW", "600")),
        help="rate-limit window in seconds (default 600)",
    )
    parser.add_argument(
        "--python", default=os.getenv("WATCHDOG_PYTHON", ""),
        help="python binary used to start main.py (default: project venv python)",
    )
    parser.add_argument(
        "--project-root", default=PROJECT_ROOT,
        help="project root containing main.py (default: this project)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="print bot/watchdog status and exit",
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="stop a running watchdog (SIGTERM) and exit",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run a single supervision iteration and exit",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.status:
        print_status(args.project_root)
        return 0
    if args.stop:
        return stop_watchdog(args.project_root)

    watchdog = Watchdog(
        args.project_root,
        interval=args.interval,
        max_restarts=args.max_restarts,
        window=args.window,
        python_cmd=args.python,
    )

    if args.once:
        watchdog._load_crash_times()
        action = watchdog.check_and_act()
        watchdog.logger.info("watchdog one-shot action: %s", action)
        return 1 if action == "halted" else 0

    return watchdog.run()


if __name__ == "__main__":
    sys.exit(main())
