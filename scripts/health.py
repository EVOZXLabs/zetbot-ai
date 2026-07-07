"""
Health Monitor for ZetBot AI.

Periodically checks real component health (internet, exchange, pipeline
data) in addition to system metrics.  Runs as a background thread.

Usage::

    from scripts.health import HealthMonitor

    monitor = HealthMonitor(logger)
    monitor.start()
    ...
    monitor.stop()
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.app_config import AppConfig
from scripts.logger import PipelineLogger

BOT_VERSION = "v0.5.0"


class HealthMonitor:
    """Periodically check real component health in a background thread."""

    def __init__(
        self,
        logger: PipelineLogger,
        config: AppConfig,
        interval: float = 60.0,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self._logger = logger
        self._config = config
        self._interval = interval
        self._shutdown_event = shutdown_event
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
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def snapshot(self) -> dict[str, Any]:
        if self._last_snapshot is None:
            self._last_snapshot = self._gather()
        return dict(self._last_snapshot)

    def force_refresh(self) -> dict[str, Any]:
        self._last_snapshot = self._gather()
        return dict(self._last_snapshot)

    def _run(self) -> None:
        while self._running:
            if self._shutdown_event is not None:
                self._shutdown_event.wait(timeout=self._interval)
                if self._shutdown_event.is_set():
                    self._running = False
                    break
            else:
                time.sleep(self._interval)
            if not self._running:
                break
            self._last_snapshot = self._gather()
            self._logger.info(f"HEALTH  {_format_metrics(self._last_snapshot)}")

    def _gather(self) -> dict[str, Any]:
        now = time.time()
        uptime_sec = now - self._start_time

        internet_ok, internet_latency = _check_internet()
        exchange_ok, exchange_name = _check_exchange(self._config.exchange)

        d = self._config.data_dir
        scanner_time, scanner_age = _file_timestamp(f"{d}/scanner_results.json", now)
        api_time, api_age = _file_timestamp(f"{d}/paper_balance.json", now)

        paper_data = _read_json(f"{d}/paper_balance.json")

        return {
            "version": BOT_VERSION,
            "uptime_sec": int(uptime_sec),
            "rss_kb": _read_rss_kb(),
            "thread_count": threading.active_count(),
            "process_cpu_sec": _read_cpu_clock_tick(),
            "internet_ok": internet_ok,
            "internet_latency_ms": internet_latency,
            "exchange_ok": exchange_ok,
            "exchange_name": exchange_name,
            "scanner_time": scanner_time,
            "scanner_age": scanner_age,
            "api_time": api_time,
            "api_age": api_age,
            "balance": paper_data.get("final_balance", 0.0),
            "equity": paper_data.get("final_equity", 0.0),
            "net_pnl": paper_data.get("net_pnl", 0.0),
            "open_positions": sum(
                1 for p in _read_json(f"{d}/positions.json").get("positions", [])
                if p.get("status") == "OPEN"
            ),
            "total_trades": paper_data.get("total_trades", 0),
            "win_rate": paper_data.get("win_rate", 0.0),
            "paused": os.path.exists(f"{d}/.paused"),
            "paper_mode": self._config.paper_mode,
            "realized_pnl": paper_data.get("realized_pnl", 0.0),
            "unrealized_pnl": paper_data.get("unrealized_pnl", 0.0),
        }


# ---------------------------------------------------------------------------
#  Component checks
# ---------------------------------------------------------------------------


def _check_internet() -> tuple[bool, float]:
    """Check internet connectivity via a fast HTTPS HEAD request."""
    t0 = time.time()
    try:
        requests.get("https://api.binance.com/api/v3/ping", timeout=5)
        latency = round((time.time() - t0) * 1000, 1)
        return True, latency
    except requests.RequestException:
        return False, 0.0


def _check_exchange(name: str) -> tuple[bool, str]:
    """Check exchange API connectivity."""
    try:
        import ccxt
        exchange_class = getattr(ccxt, name, None)
        if exchange_class is None:
            return False, "unknown"
        ex = exchange_class({"enableRateLimit": False})
        ex.load_markets()
        return True, name
    except Exception:
        return False, name


# ---------------------------------------------------------------------------
#  File helpers
# ---------------------------------------------------------------------------


def _file_timestamp(path: str, now: float) -> tuple[str, float]:
    """Return (formatted_mtime, age_seconds) for a data file."""
    try:
        mtime = os.path.getmtime(path)
        age = now - mtime
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%H:%M:%S UTC")
        return ts, age
    except OSError:
        return "N/A", float("inf")


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
#  /proc helpers (Linux only)
# ---------------------------------------------------------------------------


def _read_rss_kb() -> int:
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
    try:
        with open("/proc/self/stat") as f:
            parts = f.read().split()
        utime = float(parts[12])
        stime = float(parts[13])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return (utime + stime) / clk_tck
    except (OSError, ValueError, KeyError, IndexError):
        return 0.0


# ---------------------------------------------------------------------------
#  Formatting
# ---------------------------------------------------------------------------


def _format_metrics(m: dict[str, Any]) -> str:
    uptime = m["uptime_sec"]
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    rss_mb = m["rss_kb"] / 1024.0
    cpu_sec = m["process_cpu_sec"]
    threads = m["thread_count"]
    internet = "OK" if m["internet_ok"] else "FAIL"
    exchange = "OK" if m["exchange_ok"] else "FAIL"
    net_pnl = m.get("net_pnl", 0.0)
    return (
        f"uptime={hours:02.0f}h{minutes:02.0f}m{seconds:02.0f}s"
        f"  rss={rss_mb:.1f}MB"
        f"  cpu={cpu_sec:.1f}s"
        f"  threads={threads}"
        f"  internet={internet}"
        f"  exchange={exchange}"
        f"  net_pnl={net_pnl:+.2f}"
    )
