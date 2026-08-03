"""
Live crash-window kill helper for the BUG-1 smoke test.

Watches ``data/execution_events.jsonl`` for a take-profit (or stop-loss)
trigger and SIGKILLs the ZetBot AI process after a short delay so the
kill lands inside the crash window:

    TP_TRIGGERED -> write-ahead persisted -> market SELL submitted to the
    exchange -> _settle_live_order polls fetch_order (~3 s) ->
    EXIT_SUBMITTED -> POSITION_CLOSED

Killing ~1.5 s after TP_TRIGGERED leaves the sell order already placed on
the exchange while ``positions.json`` has NOT yet been finalized — exactly
the window a restart must recover from without re-selling the same qty.

Read-only with respect to the exchange. Never places orders.

Usage::

    python scripts/smoke_crash_kill.py --dry-run
    python scripts/smoke_crash_kill.py --delay 1.5
    python scripts/smoke_crash_kill.py --trigger SL_TRIGGERED

Exit codes: 0 = killed (or dry-run fired), 1 = fatal setup error,
130 = interrupted.
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

DEFAULT_PID_FILE = "data/zetbot.pid"
DEFAULT_EVENTS_FILE = "data/execution_events.jsonl"
DEFAULT_TRIGGER = "TP_TRIGGERED"
VALID_TRIGGERS = ("TP_TRIGGERED", "SL_TRIGGERED", "EXIT_SUBMITTED")


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _load_pid(pid_file: str) -> Optional[int]:
    path = Path(pid_file)
    if not path.is_file():
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_is_zetbot(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
    except (OSError, FileNotFoundError):
        return False
    return "main.py" in cmdline or "zetbot" in cmdline.lower()


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _event_name(line: str) -> Optional[str]:
    if not line.strip():
        return None
    try:
        return json.loads(line).get("event")
    except (ValueError, json.JSONDecodeError):
        return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kill the ZetBot process inside the TP/SL crash window.",
    )
    parser.add_argument("--pid-file", default=DEFAULT_PID_FILE,
                        help=f"bot PID file (default: {DEFAULT_PID_FILE})")
    parser.add_argument("--events-file", default=DEFAULT_EVENTS_FILE,
                        help=f"execution event log (default: {DEFAULT_EVENTS_FILE})")
    parser.add_argument("--trigger", default=DEFAULT_TRIGGER, choices=sorted(VALID_TRIGGERS),
                        help="event that arms the kill (default: TP_TRIGGERED)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds to wait after the trigger before SIGKILL (default: 1.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would happen but never kill")
    parser.add_argument("--scan-existing", action="store_true",
                        help="also arm on events already present in the log")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    pid = _load_pid(args.pid_file)
    if pid is None:
        _log(f"ERROR: no PID file at {args.pid_file} — is the bot running?")
        return 1
    if not _pid_running(pid):
        _log(f"ERROR: PID {pid} from {args.pid_file} is not running (stale PID file).")
        return 1
    if not _pid_is_zetbot(pid):
        _log(f"ERROR: PID {pid} does not look like the ZetBot process — refusing to kill.")
        return 1

    path = Path(args.events_file)
    if not path.is_file():
        _log(f"ERROR: events file not found: {args.events_file}")
        return 1

    _log(f"Watching '{args.trigger}' in {args.events_file} "
         f"— will SIGKILL PID {pid} {args.delay}s after trigger")
    if args.dry_run:
        _log("Dry-run mode: no process will be killed.")

    offset = 0 if args.scan_existing else path.stat().st_size

    try:
        while True:
            size = path.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with open(path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    for line in f:
                        if _event_name(line) == args.trigger:
                            _log(f"Trigger '{args.trigger}' detected. Waiting {args.delay}s ...")
                            time.sleep(args.delay)
                            if not _pid_running(pid):
                                _log("Bot exited on its own before the kill.")
                                return 0
                            if args.dry_run:
                                _log(f"[dry-run] would SIGKILL PID {pid} now")
                            else:
                                os.kill(pid, signal.SIGKILL)
                                _log(f"SIGKILL sent to PID {pid} — now verify on the "
                                     "exchange and positions.json (see runbook step 5).")
                            return 0
                    offset = size
            time.sleep(0.2)
    except KeyboardInterrupt:
        _log("Interrupted. Exiting without killing.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
