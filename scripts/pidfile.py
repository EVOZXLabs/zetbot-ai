"""
PID lock file to prevent duplicate ZetBot AI instances.

Usage::

    from scripts.pidfile import PidFile

    pid = PidFile("data/zetbot.pid")
    if not pid.acquire():
        print("ERROR: Another instance is already running")
        sys.exit(1)

    ... run ...

    pid.release()
"""

import os
import sys


class PidFile:
    """Manage a PID lock file for single-instance enforcement."""

    def __init__(self, path: str = "data/zetbot.pid") -> None:
        self.path: str = path
        self.pid: int = os.getpid()
        self._acquired: bool = False

    def acquire(self) -> bool:
        """Try to acquire the lock.

        Returns True if this instance now owns the lock, False if
        another instance is already running.
        """
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

        if os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    old_pid = int(f.read().strip())
            except (ValueError, OSError):
                old_pid = None

            if old_pid is not None and self._is_running(old_pid):
                return False

        try:
            with open(self.path, "w") as f:
                f.write(str(self.pid))
            self._acquired = True
            return True
        except OSError:
            return False

    def release(self) -> None:
        """Release the lock (remove PID file if owned by us)."""
        if not self._acquired:
            return
        try:
            if os.path.isfile(self.path):
                with open(self.path) as f:
                    content = f.read().strip()
                if content == str(self.pid):
                    os.remove(self.path)
        except OSError:
            pass
        self._acquired = False

    @staticmethod
    def _is_running(pid: int) -> bool:
        """Check if a process with the given PID exists."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False
