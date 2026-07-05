"""
Health Monitor for ZetBot AI.

Periodically logs system health metrics every 60 seconds while the
daemon is running.  Uses /proc filesystem (Linux) to gather memory,
CPU, and thread information without external dependencies.

Usage::

    from scripts.health import HealthMonitor

    monitor = HealthMonitor(logger)
    monitor.start()
    ...
    monitor.stop()
"""

import os
import threading
import time
from typing import Any

from scripts.logger import PipelineLogger


class HealthMonitor:
    """Periodically log system health metrics in a background thread."""

    def __init__(
        self,
        logger: PipelineLogger,
        interval: float = 60.0,
    ) -> None:
        self._logger = logger
        self._interval = interval
        self._start_time: float = time.time()
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._last_snapshot: dict[str, Any] | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="HealthMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def snapshot(self) -> dict[str, Any]:
        """Return the latest health metrics snapshot.

        Gathers metrics on first call, returns cached data thereafter
        (updated every *interval* seconds by the background thread).
        """
        if self._last_snapshot is None:
            self._last_snapshot = self._gather()
        return dict(self._last_snapshot)

    def _run(self) -> None:
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            self._last_snapshot = self._gather()
            self._logger.info(f"HEALTH  {_format_metrics(self._last_snapshot)}")

    def _gather(self) -> dict[str, Any]:
        uptime_sec = time.time() - self._start_time
        return {
            "uptime_sec": int(uptime_sec),
            "rss_kb": _read_rss_kb(),
            "thread_count": threading.active_count(),
            "process_cpu_sec": _read_cpu_clock_tick(),
        }


# ---------------------------------------------------------------------------
#  /proc helpers (Linux only)
# ---------------------------------------------------------------------------


def _read_rss_kb() -> int:
    """Return RSS in kilobytes from /proc/self/status."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
    except (OSError, ValueError):
        pass
    return 0


def _read_cpu_clock_tick() -> float:
    """Return total CPU time (utime + stime) in seconds from /proc/self/stat.

    Returns 0 on error or non-Linux systems.
    """
    try:
        with open("/proc/self/stat") as f:
            parts = f.read().split()
        # Field 13 = utime, field 14 = stime (0-indexed: 12 & 13)
        utime = float(parts[12])
        stime = float(parts[13])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return (utime + stime) / clk_tck
    except (OSError, ValueError, KeyError, IndexError):
        return 0.0


def _format_metrics(m: dict[str, Any]) -> str:
    uptime = m["uptime_sec"]
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    rss_mb = m["rss_kb"] / 1024.0
    cpu_sec = m["process_cpu_sec"]
    threads = m["thread_count"]
    return (
        f"uptime={hours:02.0f}h{minutes:02.0f}m{seconds:02.0f}s"
        f"  rss={rss_mb:.1f}MB"
        f"  cpu={cpu_sec:.1f}s"
        f"  threads={threads}"
    )
