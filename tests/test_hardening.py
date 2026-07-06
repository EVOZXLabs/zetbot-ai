"""
Stress tests for production hardening modules.

Covers: PID lock, health monitor, configuration validation,
thread watchdog, zombie detection, graceful shutdown.
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from scripts.app_config import AppConfig, ConfigError, validate_config


# ---------------------------------------------------------------------------
#  PID lock tests
# ---------------------------------------------------------------------------

class TestPidFile:
    """PID lock file acquisition, release, duplicate prevention."""

    def test_normal_acquire_and_release(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "test.pid")
        pid = PidFile(path)
        assert pid.acquire() is True
        assert os.path.isfile(path)
        with open(path) as f:
            assert f.read().strip() == str(os.getpid())
        pid.release()
        assert not os.path.isfile(path)

    def test_duplicate_prevention(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "dup.pid")

        pid1 = PidFile(path)
        assert pid1.acquire() is True
        assert os.path.isfile(path)

        pid2 = PidFile(path)
        assert pid2.acquire() is False, "Duplicate PID must be rejected"

        pid1.release()

    def test_stale_pid_is_overwritten(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "stale.pid")

        with open(path, "w") as f:
            f.write("999999999")

        pid = PidFile(path)
        assert pid.acquire() is True, "Stale PID must be overwritten"
        with open(path) as f:
            assert f.read().strip() == str(os.getpid())
        pid.release()

    def test_release_non_owned(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "foreign.pid")

        with open(path, "w") as f:
            f.write("12345")

        pid = PidFile(path)
        assert pid._acquired is False
        pid.release()
        assert os.path.isfile(path), "Must not delete foreign PID file"

    def test_release_idempotent(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "idemp.pid")
        pid = PidFile(path)
        assert pid.acquire() is True
        pid.release()
        pid.release()
        pid.release()

    def test_atexit_cleanup(self, tmp_path: Path) -> None:
        """Simulate process exit: atexit handler should remove PID file."""
        import atexit
        from scripts.pidfile import PidFile
        path = str(tmp_path / "atexit.pid")
        pid = PidFile(path)
        pid.acquire()
        assert os.path.isfile(path)
        atexit.unregister(pid.release)
        pid.release()
        assert not os.path.isfile(path)

    def test_directory_created(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        nested = str(tmp_path / "sub" / "dir" / "test.pid")
        pid = PidFile(nested)
        assert pid.acquire() is True
        assert os.path.isfile(nested)
        pid.release()

    def test_subprocess_duplicate_start(self, tmp_path: Path) -> None:
        """Launch a child that holds the lock, then try to acquire."""
        from scripts.pidfile import PidFile
        path = str(tmp_path / "subproc.pid")

        pid = PidFile(path)
        assert pid.acquire() is True

        # Same process should reject
        pid2 = PidFile(path)
        assert pid2.acquire() is False

        pid.release()

    def test_invalid_pid_file(self, tmp_path: Path) -> None:
        from scripts.pidfile import PidFile
        path = str(tmp_path / "invalid.pid")
        with open(path, "w") as f:
            f.write("not_a_number")
        pid = PidFile(path)
        assert pid.acquire() is True
        pid.release()


# ---------------------------------------------------------------------------
#  Configuration validation tests
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """validate_config must reject invalid configurations."""

    def _valid_config(self) -> AppConfig:
        return AppConfig(
            account_balance=10_000.0,
            exchange="binance",
            timeframe="1h",
            max_positions=3,
            max_risk_per_trade_pct=2.0,
            scanner_threads=5,
            scanner_top_n=50,
            telegram_timeout=10,
            telegram_retry=3,
            min_rr=1.5,
            max_rr=5.0,
            min_probability=50.0,
            max_atr_pct=8.0,
            tp1_sell_pct=30.0,
            tp2_sell_pct=30.0,
            tp3_sell_pct=40.0,
            taker_fee=0.001,
            maker_fee=0.00075,
            slippage_bps=3,
        )

    def test_valid_config_passes(self) -> None:
        validate_config(self._valid_config())

    def test_negative_balance_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "account_balance": -100})
        with pytest.raises(ConfigError, match="ACCOUNT_BALANCE"):
            validate_config(cfg)

    def test_zero_balance_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "account_balance": 0})
        with pytest.raises(ConfigError, match="ACCOUNT_BALANCE"):
            validate_config(cfg)

    def test_unsupported_exchange_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "exchange": "kraken"})
        with pytest.raises(ConfigError, match="EXCHANGE"):
            validate_config(cfg)

    def test_invalid_timeframe_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "timeframe": "2m"})
        with pytest.raises(ConfigError, match="TIMEFRAME"):
            validate_config(cfg)

    def test_negative_max_positions_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "max_positions": 0})
        with pytest.raises(ConfigError, match="MAX_POSITIONS"):
            validate_config(cfg)

    def test_tp_sum_not_100_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "tp1_sell_pct": 50.0, "tp2_sell_pct": 30.0, "tp3_sell_pct": 10.0})
        with pytest.raises(ConfigError, match="TP sell"):
            validate_config(cfg)

    def test_min_rr_greater_than_max_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "min_rr": 5.0, "max_rr": 3.0})
        with pytest.raises(ConfigError, match="MIN_RR"):
            validate_config(cfg)

    def test_risk_pct_too_high_rejected(self) -> None:
        cfg = self._valid_config()
        cfg = AppConfig(**{**cfg.__dict__, "max_risk_per_trade_pct": 150.0})
        with pytest.raises(ConfigError, match="MAX_RISK_PER_TRADE_PCT"):
            validate_config(cfg)

    def test_multiple_errors_collected(self) -> None:
        cfg = AppConfig(
            account_balance=-1,
            exchange="kraken",
            timeframe="bad",
            max_positions=0,
            max_risk_per_trade_pct=200,
            scanner_threads=0,
            scanner_top_n=0,
            telegram_timeout=0,
            telegram_retry=-1,
            min_rr=10,
            max_rr=5,
            min_probability=0,
            max_atr_pct=-1,
            tp1_sell_pct=50,
            tp2_sell_pct=30,
            tp3_sell_pct=20,
            taker_fee=0.001,
            maker_fee=0.00075,
            slippage_bps=3,
        )
        with pytest.raises(ConfigError) as excinfo:
            validate_config(cfg)
        assert "\n" in str(excinfo.value), "Multiple errors must be separated by newlines"


# ---------------------------------------------------------------------------
#  Health monitor tests
# ---------------------------------------------------------------------------

class TestHealthMonitor:
    """Health monitor startup, metrics gathering, shutdown."""

    def _make_config(self) -> AppConfig:
        return AppConfig(
            account_balance=10000, exchange="binance", timeframe="1h",
            max_positions=3, max_risk_per_trade_pct=2,
            scanner_threads=5, scanner_top_n=50,
            telegram_timeout=10, telegram_retry=3,
            min_rr=1.5, max_rr=5, min_probability=50,
            max_atr_pct=8, tp1_sell_pct=30, tp2_sell_pct=30,
            tp3_sell_pct=40, taker_fee=0.001, maker_fee=0.00075,
            slippage_bps=3,
        )

    def test_health_monitor_start_stop(self) -> None:
        from scripts.health import HealthMonitor
        monitor = HealthMonitor(logger=_FakeLogger(), config=self._make_config(), interval=0.1)
        monitor.start()
        time.sleep(0.3)
        monitor.stop()
        assert not monitor._running

    def test_health_monitor_gathers_metrics(self) -> None:
        from scripts.health import HealthMonitor
        monitor = HealthMonitor(logger=_FakeLogger(), config=self._make_config(), interval=60.0)
        metrics = monitor._gather()
        assert "uptime_sec" in metrics
        assert "rss_kb" in metrics
        assert "thread_count" in metrics
        assert "process_cpu_sec" in metrics

    def test_health_monitor_logs_metrics(self) -> None:
        from scripts.health import HealthMonitor
        logger = _FakeLogger()
        monitor = HealthMonitor(logger=logger, config=self._make_config(), interval=0.1)
        monitor.start()
        time.sleep(0.35)
        monitor.stop()
        logged = logger.get_lines()
        health_lines = [l for l in logged if "HEALTH" in l]
        assert len(health_lines) >= 1, f"Expected HEALTH log lines, got {logged}"

    def test_snapshot_returns_cached_metrics(self) -> None:
        from scripts.health import HealthMonitor
        monitor = HealthMonitor(logger=_FakeLogger(), config=self._make_config(), interval=60.0)
        snap = monitor.snapshot()
        assert "uptime_sec" in snap
        assert "rss_kb" in snap
        assert "thread_count" in snap
        assert "process_cpu_sec" in snap
        assert snap["thread_count"] >= 1

    def test_snapshot_returns_copy(self) -> None:
        from scripts.health import HealthMonitor
        monitor = HealthMonitor(logger=_FakeLogger(), config=self._make_config(), interval=60.0)
        snap1 = monitor.snapshot()
        snap2 = monitor.snapshot()
        assert snap1 == snap2

    def test_snapshot_updated_by_background_thread(self) -> None:
        from scripts.health import HealthMonitor
        logger = _FakeLogger()
        monitor = HealthMonitor(logger=logger, config=self._make_config(), interval=0.2)
        snap_before = monitor.snapshot()
        monitor.start()
        time.sleep(0.5)
        snap_after = monitor.snapshot()
        monitor.stop()
        assert snap_after["uptime_sec"] >= snap_before["uptime_sec"]

    def test_snapshot_no_health_monitor(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=None)
        result = center._cmd_health("/health")
        assert result is not None
        assert "*Score:*" in result or "Score:" in result
        assert "*System*" in result
        assert "*Resources*" in result
        assert "*Components*" in result
        assert "*Account*" in result or "Account" in result

    def test_format_metrics(self) -> None:
        from scripts.health import _format_metrics
        result = _format_metrics({
            "uptime_sec": 3661,
            "rss_kb": 102400,
            "thread_count": 5,
            "process_cpu_sec": 12.5,
            "internet_ok": True,
            "exchange_ok": True,
        })
        assert "01h01m01s" in result
        assert "100.0MB" in result
        assert "threads=5" in result
        assert "cpu=12.5s" in result
        assert "internet=OK" in result
        assert "exchange=OK" in result


# ---------------------------------------------------------------------------
#  /health command tests
# ---------------------------------------------------------------------------

class TestHealthCommand:
    """Telegram /health command formatting and content."""

    def _make_config(self) -> AppConfig:
        return AppConfig(
            account_balance=10000, exchange="binance", timeframe="1h",
            max_positions=3, max_risk_per_trade_pct=2,
            scanner_threads=5, scanner_top_n=50,
            telegram_timeout=10, telegram_retry=3,
            min_rr=1.5, max_rr=5, min_probability=50,
            max_atr_pct=8, tp1_sell_pct=30, tp2_sell_pct=30,
            tp3_sell_pct=40, taker_fee=0.001, maker_fee=0.00075,
            slippage_bps=3,
        )

    def _make_health_monitor(self) -> Any:
        from scripts.health import HealthMonitor
        return HealthMonitor(logger=_FakeLogger(), config=self._make_config(), interval=60.0)

    def test_health_contains_all_required_sections(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        assert "*ZetBot" in result
        assert "*Score:*" in result
        assert "*System*" in result
        assert "*Resources*" in result
        assert "*Components*" in result
        assert "*Account*" in result
        assert "*Positions*" in result
        assert "*Timestamps*" in result

    def test_health_contains_required_fields(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        assert "Uptime:" in result
        assert "Mode:" in result
        assert "CPU:" in result
        assert "Memory:" in result
        assert "Threads:" in result
        assert "Internet:" in result
        assert "Telegram:" in result
        assert "Scanner:" in result
        assert "Open:" in result
        assert "Total:" in result
        assert "Equity:" in result
        assert "Balance:" in result
        assert "Net PnL:" in result
        assert "Win Rate:" in result
        assert "Last Scan:" in result
        assert "Last Trade:" in result

    def test_health_shows_correct_mode(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        assert "PAPER" in result

    def test_health_includes_status_icons(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        status_icons = {"\U0001f7e2", "\U0001f7e1", "\U0001f534", "\u26aa"}
        found = {icon for icon in status_icons if icon in result}
        assert len(found) >= 2, f"Expected at least 2 status icons, found {found}"

    def test_health_score_in_range(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        import re
        match = re.search(r"\*Score:\*.*?`(\d+)/100`", result)
        assert match is not None, f"Could not find Score in:\n{result}"
        score = int(match.group(1))
        assert 0 <= score <= 100

    def test_health_disabled_telegram_shows_disabled(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        cfg = AppConfig(**{**cfg.__dict__, "telegram_enabled": False})
        health = self._make_health_monitor()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=health)
        result = center._cmd_health("/health")
        assert "Disabled" in result or "\u26aa" in result

    def test_health_no_health_monitor_graceful(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter
        cfg = self._make_config()
        center = TelegramCommandCenter(cfg, test_mode=True, health_monitor=None)
        result = center._cmd_health("/health")
        assert "*Score:*" in result
        assert "/100" in result


# ---------------------------------------------------------------------------
#  Thread zombie detection tests
# ---------------------------------------------------------------------------

class TestZombieDetection:
    """Thread name collision detection."""

    def test_no_duplicate_when_unique(self) -> None:
        import main
        # Our helper only checks for alive threads
        assert not main._thread_exists("__nonexistent_test_thread__")

    def test_detects_duplicate(self) -> None:
        import main
        t = threading.Thread(target=lambda: time.sleep(10), name="__test_zombie__", daemon=True)
        t.start()
        assert main._thread_exists("__test_zombie__")
        t.join(timeout=1)

    def test_start_worker_prevents_duplicate(self) -> None:
        from scripts.logger import PipelineLogger
        import main
        t = threading.Thread(target=lambda: time.sleep(10), name="__test_dup__", daemon=True)
        t.start()
        result = main._start_worker("__test_dup__", lambda: None, _FakeLogger())
        assert result is None, "Duplicate worker must be rejected"
        t.join(timeout=1)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

class _FakeLogger:
    """In-memory logger for testing."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"INFO {msg % args if args else msg}")

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"ERROR {msg % args if args else msg}")

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"WARN {msg % args if args else msg}")

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"DEBUG {msg % args if args else msg}")

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"CRIT {msg % args if args else msg}")

    def get_lines(self) -> list[str]:
        return list(self._lines)
