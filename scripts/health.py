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
from scripts.position_status import is_open

BOT_VERSION = "v0.5.0"
SCANNER_TIMEOUT = 7200        # seconds before scanner data is considered stale (default 2h)
SCANNER_CRITICAL = 86400      # seconds before scanner data is considered critical (default 24h)

# Internet probes — deliberately NOT api.binance.com: Binance is blocked
# by Indonesian ISPs (Kominfo), so a binance-based probe reported FAIL on
# a perfectly healthy connection and pushed users toward unnecessary VPNs.
# These neutral endpoints are reachable in Indonesia (and everywhere else);
# first success wins, so a single flaky probe never trips the health check.
_INTERNET_PROBES = (
    "https://www.gstatic.com/generate_204",
    "https://1.1.1.1",
    "https://www.google.com",
)


class HealthMonitor:
    """Periodically check real component health in a background thread."""

    def __init__(
        self,
        logger: PipelineLogger,
        config: AppConfig,
        interval: float = 60.0,
        shutdown_event: threading.Event | None = None,
        wallet: Any = None,
    ) -> None:
        self._logger = logger
        self._config = config
        self._interval = interval
        self._shutdown_event = shutdown_event
        self._wallet = wallet
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
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

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
        telegram_status = _read_telegram_status(f"{d}/telegram_status.json")
        scanner_time, scanner_age = _file_timestamp(f"{d}/scanner_results.json", now)
        api_time, api_age = _file_timestamp(f"{d}/paper_balance.json", now)

        paper_data = _read_json(f"{d}/paper_balance.json")

        # net_pnl/balance/equity are the canonical snapshot values
        # (computed from raw data via MetricsManager.compute_snapshot),
        # never the raw keys of paper_balance.json — those are only
        # refreshed on position closure and can sit stale (or absent)
        # for long stretches, which made HEALTH report figures that
        # matched nothing in the actual account or in /wallet.
        try:
            from scripts.metrics_manager import MetricsManager  # noqa: PLC0415
            snap = MetricsManager(
                self._config.data_dir,
                wallet=self._wallet,
                mode_provider=lambda: (
                    "PAPER" if self._config.paper_mode else "LIVE"
                ),
            ).account()
            net_pnl = round(snap.net_pnl, 2)
            balance = round(snap.balance, 2)
            equity = round(snap.equity, 2)
        except Exception:
            net_pnl = paper_data.get("net_pnl", 0.0)
            balance = paper_data.get("final_balance", 0.0)
            equity = paper_data.get("final_equity", 0.0)

        # Derive scanner status
        if scanner_age == float("inf"):
            scanner_status = "no_data"
        elif scanner_age < SCANNER_TIMEOUT:
            scanner_status = "healthy"
        elif scanner_age < SCANNER_CRITICAL:
            scanner_status = "stale"
        else:
            scanner_status = "critical"

        score = _compute_health_score(
            internet_ok, exchange_ok, scanner_status,
            scanner_age, api_age, uptime_sec,
        )

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
            "telegram_status": telegram_status,
            "scanner_time": scanner_time,
            "scanner_age": scanner_age,
            "scanner_status": scanner_status,
            "scanner_timeout": SCANNER_TIMEOUT,
            "scanner_critical": SCANNER_CRITICAL,
            "api_time": api_time,
            "api_age": api_age,
            "balance": balance,
            "equity": equity,
            "net_pnl": net_pnl,
            "quote_currency": self._config.quote_currency,
            "open_positions": sum(
                1 for p in _read_json(f"{d}/positions.json").get("positions", [])
                if is_open(p.get("status"))
            ),
            "total_trades": paper_data.get("total_trades", 0),
            "win_rate": paper_data.get("win_rate", 0.0),
            "paused": os.path.exists(f"{d}/.paused"),
            "paper_mode": self._config.paper_mode,
            "realized_pnl": paper_data.get("realized_pnl", 0.0),
            "unrealized_pnl": paper_data.get("unrealized_pnl", 0.0),
            "health_score": score,
        }


# ---------------------------------------------------------------------------
#  Component checks
# ---------------------------------------------------------------------------


def _check_internet() -> tuple[bool, float]:
    """Check internet connectivity via fast HTTPS probes.

    Probes a small list of neutral, exchange-independent endpoints
    (Google generate_204, Cloudflare 1.1.1.1, google.com) instead of a
    single exchange API — those are geo-blocked in several regions
    (e.g. Binance in Indonesia), which made a working connection report
    ``internet=FAIL`` and implied a VPN was needed when it wasn't.
    """
    t0 = time.time()
    for url in _INTERNET_PROBES:
        try:
            requests.get(url, timeout=5)
            latency = round((time.time() - t0) * 1000, 1)
            return True, latency
        except requests.RequestException:
            continue
    return False, 0.0


_exchange_check_cache: dict[str, tuple[float, tuple[bool, str]]] = {}
_EXCHANGE_CHECK_TTL = 120.0


def _check_exchange(name: str) -> tuple[bool, str]:
    """Check exchange API connectivity (cached 2 minutes).

    The check loads all markets (``/api/pairs`` on indodax) — running it
    every 60s on top of the monitor/pipeline ticker fetches used to trip
    the exchange rate limit (429), so results are cached with a TTL and
    the check goes through the shared cached client.
    """
    now = time.time()
    cached = _exchange_check_cache.get(name)
    if cached is not None and now - cached[0] < _EXCHANGE_CHECK_TTL:
        return cached[1]
    try:
        from bot.data import get_cached_public_exchange  # noqa: PLC0415
        ex = get_cached_public_exchange(name)
        ex.load_markets()
        result = (True, name)
    except Exception:
        result = (False, name)
    _exchange_check_cache[name] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
#  File helpers
# ---------------------------------------------------------------------------


def _file_timestamp(path: str, now: float) -> tuple[str, float]:
    """Return (ISO timestamp, age_seconds) for a data file."""
    try:
        mtime = os.path.getmtime(path)
        age = now - mtime
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        return ts, age
    except OSError:
        return "N/A", float("inf")


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_telegram_status(path: str) -> str:
    """Read the Telegram link health written by the polling loop.

    Returns ``OK``/``DEGRADED``/``OFFLINE``, or ``UNKNOWN`` when the
    Telegram command center is not running or has not reported yet.
    """
    data = _read_json(path)
    status = data.get("status", "")
    return status if status in ("OK", "DEGRADED", "OFFLINE") else "UNKNOWN"


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


def _compute_health_score(
    internet_ok: bool,
    exchange_ok: bool,
    scanner_status: str,
    scanner_age: float,
    api_age: float,
    uptime_sec: float,
) -> int:
    score = 100
    if not internet_ok:
        score -= 15
    if not exchange_ok:
        score -= 20
    if scanner_status == "stale":
        score -= 10
    elif scanner_status == "critical" or scanner_status == "no_data":
        score -= 20
    if api_age == float("inf"):
        score -= 15
    elif api_age > 3600:
        score -= 5
    if uptime_sec < 60:
        score -= 5
    return max(0, score)


def _format_metrics(m: dict[str, Any]) -> str:
    uptime = m["uptime_sec"]
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    rss_mb = m["rss_kb"] / 1024.0
    cpu_sec = m["process_cpu_sec"]
    threads = m["thread_count"]
    internet = "OK" if m["internet_ok"] else "FAIL"
    exchange = "OK" if m["exchange_ok"] else "FAIL"
    telegram = m.get("telegram_status", "UNKNOWN")
    net_pnl = m.get("net_pnl", 0.0)
    quote = m.get("quote_currency", "USDT")
    return (
        f"uptime={hours:02.0f}h{minutes:02.0f}m{seconds:02.0f}s"
        f"  rss={rss_mb:.1f}MB"
        f"  cpu={cpu_sec:.1f}s"
        f"  threads={threads}"
        f"  internet={internet}"
        f"  exchange={exchange}"
        f"  telegram={telegram}"
        f"  net_pnl={net_pnl:+.2f} {quote}"
    )
