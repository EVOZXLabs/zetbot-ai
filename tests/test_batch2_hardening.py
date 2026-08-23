"""Batch 2+3 production-hardening regression tests.

Covers:
  2.1  BackupScheduler — hourly backup + 7-day retention pruning.
  2.2  Watchdog heartbeat and pipeline-staleness checks.
  2.3  pipeline_last_run.json and heartbeat file written on pipeline success.
  2.4  Telegram notification sent on pipeline failure.
  2.5  PipelineLogger rotation — rolls to new file on date change, prunes old logs.
  3.1  /health command shows CPU/RAM/uptime/exchange/pipeline fields.
  3.2  DailyReportScheduler wakes at 00:00 WIB and calls notify_daily_summary.
  3.3  docs/CRASH_RECOVERY_TEST.md exists with all 6 scenarios documented.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    os.makedirs("backups", exist_ok=True)
    os.makedirs("logs", exist_ok=True)


def _write_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


# ===========================================================================
#  2.1 — BackupScheduler + prune_old_backups
# ===========================================================================


class TestBackupScheduler:

    def test_prune_removes_old_files(self, tmp_path: Any) -> None:
        from scripts.backup_restore import prune_old_backups

        backup_dir = str(tmp_path / "backups")
        os.makedirs(backup_dir, exist_ok=True)  # autouse fixture may create it

        # Old file (8 days ago)
        old = os.path.join(backup_dir, "backup-old.zip")
        with open(old, "w") as f:
            f.write("old")
        old_mtime = time.time() - 8 * 86400
        os.utime(old, (old_mtime, old_mtime))

        # Recent file (1 day ago)
        new = os.path.join(backup_dir, "backup-new.zip")
        with open(new, "w") as f:
            f.write("new")

        removed = prune_old_backups(backup_dir=backup_dir, retention_days=7)

        assert old in removed
        assert not os.path.exists(old), "Old backup must be deleted"
        assert os.path.exists(new), "Recent backup must be kept"

    def test_prune_keeps_recent_files(self, tmp_path: Any) -> None:
        from scripts.backup_restore import prune_old_backups

        backup_dir = str(tmp_path / "backups2")
        os.makedirs(backup_dir)

        recent = os.path.join(backup_dir, "backup-recent.zip")
        with open(recent, "w") as f:
            f.write("recent")

        removed = prune_old_backups(backup_dir=backup_dir, retention_days=7)
        assert not removed
        assert os.path.exists(recent)

    def test_prune_graceful_on_missing_dir(self) -> None:
        from scripts.backup_restore import prune_old_backups

        removed = prune_old_backups(backup_dir="/nonexistent/dir", retention_days=7)
        assert removed == []

    def test_scheduler_start_stop(self) -> None:
        from scripts.backup_restore import BackupScheduler

        shutdown = threading.Event()
        sched = BackupScheduler(
            interval_seconds=9999,   # don't fire during test
            retention_days=7,
            shutdown_event=shutdown,
        )
        # Patch backup creation so no files are written
        with patch("scripts.backup_restore.create_backup", return_value="fake.zip"):
            sched.start()
            time.sleep(0.2)
            assert sched._thread is not None
            assert sched._thread.is_alive()
            sched.stop()

    def test_backup_now_creates_and_prunes(self, tmp_path: Any) -> None:
        from scripts.backup_restore import BackupScheduler

        backup_dir = str(tmp_path / "bk")
        os.makedirs(backup_dir)

        # Old file to prune
        old = os.path.join(backup_dir, "backup-old.zip")
        with open(old, "w") as f:
            f.write("x")
        os.utime(old, (time.time() - 10 * 86400,) * 2)

        sched = BackupScheduler(backup_dir=backup_dir, retention_days=7)

        with patch("scripts.backup_restore.create_backup", return_value="new.zip") as mock_cb:
            result = sched.backup_now()

        assert result == "new.zip"
        mock_cb.assert_called_once()
        assert not os.path.exists(old), "Old backup must be pruned after backup_now()"


# ===========================================================================
#  2.2 — Watchdog heartbeat and pipeline-staleness
# ===========================================================================


class TestWatchdogHeartbeat:

    def _make_watchdog(self, project_root: str):
        from scripts.watchdog import Watchdog
        return Watchdog(project_root=project_root, interval=5.0)

    def test_heartbeat_age_returns_none_when_file_missing(self, tmp_path: Any) -> None:
        wd = self._make_watchdog(str(tmp_path))
        age = wd._heartbeat_age()
        assert age is None

    def test_heartbeat_age_returns_seconds(self, tmp_path: Any) -> None:
        from scripts.watchdog import HEARTBEAT_FILE

        wd = self._make_watchdog(str(tmp_path))
        path = tmp_path / HEARTBEAT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        # mtime is now; age should be ~0
        age = wd._heartbeat_age()
        assert age is not None
        assert 0.0 <= age < 5.0

    def test_pipeline_age_returns_none_when_file_missing(self, tmp_path: Any) -> None:
        wd = self._make_watchdog(str(tmp_path))
        age = wd._pipeline_age()
        assert age is None

    def test_pipeline_age_returns_seconds(self, tmp_path: Any) -> None:
        from scripts.watchdog import PIPELINE_LAST_RUN_FILE

        wd = self._make_watchdog(str(tmp_path))
        path = tmp_path / PIPELINE_LAST_RUN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        age = wd._pipeline_age()
        assert age is not None
        assert 0.0 <= age < 5.0

    def test_check_and_act_restarts_on_stale_heartbeat(self, tmp_path: Any) -> None:
        from scripts.watchdog import Watchdog, HEARTBEAT_STALE_SECONDS

        wd = Watchdog(project_root=str(tmp_path), interval=5.0)
        wd._bot_running = lambda: True           # bot appears alive
        wd._heartbeat_age = lambda: HEARTBEAT_STALE_SECONDS + 60  # stale!
        wd._pipeline_age = lambda: None
        wd._crash_rate_exceeded = lambda: False
        wd._register_crash = lambda: None
        wd._kill_bot = MagicMock()
        wd._do_restart = MagicMock()
        wd._notify = MagicMock()

        action = wd.check_and_act()

        assert action == "heartbeat_stale"
        wd._kill_bot.assert_called_once()
        wd._do_restart.assert_called_once()

    def test_check_and_act_running_when_heartbeat_fresh(self, tmp_path: Any) -> None:
        from scripts.watchdog import Watchdog

        wd = Watchdog(project_root=str(tmp_path), interval=5.0)
        wd._bot_running = lambda: True
        wd._heartbeat_age = lambda: 30.0   # fresh
        wd._pipeline_age = lambda: 60.0    # fine
        wd._notify = MagicMock()

        action = wd.check_and_act()
        assert action == "running"


# ===========================================================================
#  2.3 — pipeline_last_run.json + heartbeat written on pipeline success
# ===========================================================================


class TestPipelineTimestamps:

    def test_write_pipeline_timestamp(self) -> None:
        from scripts.pipeline_scheduler import _write_pipeline_timestamp, PIPELINE_LAST_RUN_FILE

        _write_pipeline_timestamp()
        assert os.path.exists(PIPELINE_LAST_RUN_FILE)
        with open(PIPELINE_LAST_RUN_FILE) as f:
            data = json.load(f)
        assert "last_run" in data

    def test_write_heartbeat(self) -> None:
        from scripts.pipeline_scheduler import write_heartbeat, HEARTBEAT_FILE

        write_heartbeat()
        assert os.path.exists(HEARTBEAT_FILE)
        with open(HEARTBEAT_FILE) as f:
            data = json.load(f)
        assert "ts" in data

    def test_execute_pipeline_writes_last_run_on_success(self) -> None:
        from scripts.pipeline_scheduler import PipelineScheduler, PIPELINE_LAST_RUN_FILE

        result = MagicMock()
        result.success = True
        pipeline_fn = MagicMock(return_value=[result])

        sched = PipelineScheduler(pipeline_fn=pipeline_fn, interval=300)
        sched._running = True   # must be True for _execute_pipeline to run
        sched._execute_pipeline()

        assert os.path.exists(PIPELINE_LAST_RUN_FILE)

    def test_execute_pipeline_notifies_on_failure(self) -> None:
        from scripts.pipeline_scheduler import PipelineScheduler

        pipeline_fn = MagicMock(side_effect=RuntimeError("oops"))
        sched = PipelineScheduler(pipeline_fn=pipeline_fn, interval=300)
        sched._running = True

        with patch("scripts.pipeline_scheduler._notify_pipeline_error") as mock_notify:
            sched._execute_pipeline()

        mock_notify.assert_called_once()
        assert "oops" in mock_notify.call_args[0][0]


# ===========================================================================
#  2.4 — Telegram critical error notification on pipeline failure
# ===========================================================================


class TestTelegramCriticalErrors:

    def test_notify_pipeline_error_calls_notifier(self) -> None:
        from scripts.pipeline_scheduler import _notify_pipeline_error

        # _notify_pipeline_error imports Notifier lazily from bot.notifier
        with patch("bot.notifier.Notifier") as MockNotifier:
            instance = MagicMock()
            MockNotifier.from_env.return_value = instance
            _notify_pipeline_error("exchange timeout")

        instance.notify_error.assert_called_once()
        call_arg = instance.notify_error.call_args[0][0]
        assert "exchange timeout" in call_arg

    def test_notify_pipeline_error_does_not_raise(self) -> None:
        """Error notification must never crash the scheduler."""
        from scripts.pipeline_scheduler import _notify_pipeline_error

        # Simulate total import failure — must not raise
        with patch("builtins.__import__", side_effect=lambda *a, **k: (_ for _ in ()).throw(ImportError("no module")) if "bot.notifier" in str(a) else __builtins__.__import__(*a, **k) if False else __import__(*a, **k)):
            pass  # just verify the function doesn't crash with a bad notifier

        # Simpler: patch notify_error to raise; verify function still doesn't propagate
        with patch("bot.notifier.Notifier") as MockNotifier:
            instance = MagicMock()
            instance.notify_error.side_effect = RuntimeError("telegram down")
            MockNotifier.from_env.return_value = instance
            _notify_pipeline_error("something bad")  # must not raise


# ===========================================================================
#  2.5 — PipelineLogger rotation
# ===========================================================================


class TestLogRotation:

    def _make_logger(self, tmp_path: Any):
        from scripts.app_config import AppConfig
        from scripts.logger import PipelineLogger
        cfg = AppConfig(logs_dir=str(tmp_path / "logs"))
        os.makedirs(cfg.logs_dir, exist_ok=True)
        return PipelineLogger(cfg), cfg

    def test_log_written_to_todays_file(self, tmp_path: Any) -> None:
        logger, cfg = self._make_logger(tmp_path)
        logger.info("hello")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = os.path.join(cfg.logs_dir, f"{today}.log")
        assert os.path.exists(log_file)
        with open(log_file) as f:
            assert "hello" in f.read()

    def test_rotates_on_date_change(self, tmp_path: Any) -> None:
        from scripts.logger import PipelineLogger
        from scripts.app_config import AppConfig

        cfg = AppConfig(logs_dir=str(tmp_path / "logs"))
        os.makedirs(cfg.logs_dir, exist_ok=True)
        logger = PipelineLogger(cfg)

        # Simulate the log_date being yesterday
        logger._log_date = "2000-01-01"
        logger._log_path = os.path.join(cfg.logs_dir, "2000-01-01.log")

        logger.info("new day message")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_log = os.path.join(cfg.logs_dir, f"{today}.log")
        assert os.path.exists(new_log), f"Expected rotated log at {new_log}"
        with open(new_log) as f:
            assert "new day message" in f.read()

    def test_prunes_old_logs(self, tmp_path: Any) -> None:
        from scripts.logger import PipelineLogger
        from scripts.app_config import AppConfig

        cfg = AppConfig(logs_dir=str(tmp_path / "logs"))
        os.makedirs(cfg.logs_dir, exist_ok=True)
        logger = PipelineLogger(cfg)

        # Create fake old log files (15 days ago)
        old_log = os.path.join(cfg.logs_dir, "2020-01-01.log")
        with open(old_log, "w") as f:
            f.write("old")
        old_mtime = time.time() - 15 * 86400
        os.utime(old_log, (old_mtime, old_mtime))

        logger._prune_old_logs()

        assert not os.path.exists(old_log), "Old log must be pruned"

    def test_keeps_recent_logs(self, tmp_path: Any) -> None:
        from scripts.logger import PipelineLogger
        from scripts.app_config import AppConfig

        cfg = AppConfig(logs_dir=str(tmp_path / "logs"))
        os.makedirs(cfg.logs_dir, exist_ok=True)
        logger = PipelineLogger(cfg)

        recent_log = os.path.join(cfg.logs_dir, "recent.log")
        with open(recent_log, "w") as f:
            f.write("recent")

        logger._prune_old_logs()

        assert os.path.exists(recent_log), "Recent log must be kept"


# ===========================================================================
#  3.1 — /health command completeness
# ===========================================================================


class TestHealthCommandCompleteness:

    def _make_snapshot(self) -> dict:
        return {
            "version": "v0.5.1",
            "uptime_sec": 3600,
            "rss_kb": 51200,
            "thread_count": 12,
            "process_cpu_sec": 5.0,
            "internet_ok": True,
            "internet_latency_ms": 50.0,
            "exchange_ok": True,
            "exchange_name": "binance",
            "telegram_status": "OK",
            "scanner_time": "N/A",
            "scanner_age": 100.0,
            "scanner_status": "healthy",
            "scanner_timeout": 7200,
            "scanner_critical": 86400,
            "api_time": "N/A",
            "api_age": 60.0,
            "balance": 10000.0,
            "equity": 10500.0,
            "net_pnl": 500.0,
            "quote_currency": "USDT",
            "open_positions": 1,
            "total_trades": 5,
            "win_rate": 80.0,
            "paused": False,
            "paper_mode": True,
            "realized_pnl": 500.0,
            "unrealized_pnl": 0.0,
            "health_score": 95,
        }

    def test_health_snapshot_has_required_fields(self) -> None:
        snap = self._make_snapshot()
        required = [
            "uptime_sec", "rss_kb", "thread_count", "process_cpu_sec",
            "internet_ok", "exchange_ok", "net_pnl", "balance", "equity",
            "open_positions", "health_score",
        ]
        for field in required:
            assert field in snap, f"Missing required health field: {field}"

    def test_health_command_executes_without_error(self) -> None:
        from telegram.commands.health import HealthCommand
        from telegram.context import CommandContext
        from scripts.app_config import AppConfig

        ctx = CommandContext(config=AppConfig())
        monitor = MagicMock()
        monitor.force_refresh.return_value = self._make_snapshot()
        ctx.health_monitor = monitor

        cmd = HealthCommand()
        result = cmd.execute(ctx, "")
        assert isinstance(result, str)
        assert len(result) > 10

    def test_health_format_includes_cpu_ram_uptime(self) -> None:
        from scripts.health import _format_metrics

        metrics = self._make_snapshot()
        line = _format_metrics(metrics)
        assert "uptime" in line
        assert "rss" in line or "MB" in line
        assert "cpu" in line


# ===========================================================================
#  3.2 — DailyReportScheduler
# ===========================================================================


class TestDailyReportScheduler:

    def test_seconds_until_midnight_wib_positive(self) -> None:
        from scripts.daily_report import _seconds_until_midnight_wib

        secs = _seconds_until_midnight_wib()
        assert 0 < secs <= 86400

    def test_send_now_calls_notify_daily_summary(self) -> None:
        from scripts.daily_report import DailyReportScheduler

        notifier = MagicMock()
        _write_json("data/paper_balance.json", {
            "final_balance": 10_000.0,
            "total_trades": 3,
        })

        sched = DailyReportScheduler(notifier=notifier, data_dir="data")
        sched.send_now()

        notifier.notify_daily_summary.assert_called_once()
        _stats, _balance = notifier.notify_daily_summary.call_args[0]
        assert isinstance(_stats, dict)
        assert isinstance(_balance, float)

    def test_scheduler_start_stop(self) -> None:
        from scripts.daily_report import DailyReportScheduler

        notifier = MagicMock()
        shutdown = threading.Event()
        sched = DailyReportScheduler(
            notifier=notifier,
            data_dir="data",
            shutdown_event=shutdown,
        )

        with patch("scripts.daily_report._seconds_until_midnight_wib", return_value=9999):
            sched.start()
            time.sleep(0.15)
            assert sched._thread is not None and sched._thread.is_alive()
            sched.stop()

    def test_send_now_does_not_raise_on_missing_data(self) -> None:
        from scripts.daily_report import DailyReportScheduler

        notifier = MagicMock()
        sched = DailyReportScheduler(notifier=notifier, data_dir="data")
        sched.send_now()   # no data files exist — must not raise

    def test_gather_stats_returns_dict_and_balance(self) -> None:
        from scripts.daily_report import DailyReportScheduler

        _write_json("data/paper_balance.json", {
            "final_balance": 50_000.0,
        })
        sched = DailyReportScheduler(notifier=MagicMock(), data_dir="data")
        stats, balance = sched._gather_stats()

        assert isinstance(stats, dict)
        assert balance == pytest.approx(50_000.0)

    def test_gather_stats_uses_wib_day_boundary(self) -> None:
        """The 00:00 WIB report must read the previous WIB day's stats, never the
        UTC-based today_summary() (BUG-2 regression guard)."""
        from scripts.daily_report import DailyReportScheduler

        _write_json("data/paper_balance.json", {"final_balance": 100.0})

        mgr = MagicMock()
        mgr._summarize_trades.return_value = {
            "total_trades": 4, "wins": 3, "losses": 1,
            "win_rate": 75.0, "pnl": 12.5,
        }
        mgr.trades_of_previous_wib_day.return_value = [{"pnl": 1}]
        with patch("scripts.metrics_manager.MetricsManager", return_value=mgr):
            sched = DailyReportScheduler(notifier=MagicMock(), data_dir="data")
            stats, balance = sched._gather_stats()

        assert stats["total_trades"] == 4
        assert stats["win_count"] == 3
        assert stats["loss_count"] == 1
        assert stats["win_rate"] == 75.0
        assert stats["total_profit"] == 12.5
        mgr.trades_of_previous_wib_day.assert_called_once()
        mgr.today_summary_wib.assert_not_called()
        mgr.today_summary.assert_not_called()


# ===========================================================================
#  3.2b — WIB day boundary (BUG-2): trades 00:00-07:00 WIB must count
# ===========================================================================


class TestMetricsWibDayBoundary:

    def test_wib_boundary_includes_early_morning_wib_trades(
        self, tmp_path: Any,
    ) -> None:
        import datetime as _dt
        from scripts import metrics_manager as mm_mod
        from scripts.metrics_manager import MetricsManager

        # Fixed "now": 2026-08-06 02:00 UTC == 09:00 WIB (same WIB day).
        fixed_utc = _dt.datetime(2026, 8, 6, 2, 0, 0, tzinfo=_dt.timezone.utc)

        class _FakeDatetime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed_utc
                return fixed_utc.astimezone(tz)

        def _order(side: str, ts: str, pnl: float) -> dict[str, Any]:
            # Unique order id is REQUIRED: trade_history() now reads the
            # rebuilt paper_trade_history.csv, and rebuild_trade_history_csv
            # dedupes CLOSED SELL orders by id — without distinct ids the
            # three SELL orders below would collapse into a single trade.
            _order.n += 1
            return {
                "id": f"ord-{_order.n}", "symbol": "BTC/USDT", "side": side,
                "status": "CLOSED", "quantity": 1.0, "filled_quantity": 1.0,
                "fill_price": 100.0, "net_pnl": pnl, "net_pnl_pct": 1.0,
                "holding_hours": 1.0, "created_at": ts, "filled_at": ts,
                "closed_at": ts,
            }
        _order.n = 0

        # 01:00 WIB Aug 6 == 18:00 UTC Aug 5 — BEFORE UTC midnight, so the
        # UTC day excludes it; AFTER WIB midnight (17:00 UTC Aug 5), so the
        # WIB day includes it. This is exactly the window the daily report
        # used to drop.
        early_wib = "2026-08-05T18:00:00.000000+00:00"
        # 08:30 WIB Aug 6 == 01:30 UTC Aug 6 — inside both boundaries.
        mid_wib = "2026-08-06T01:30:00.000000+00:00"
        # 10:00 UTC Aug 5 — inside neither.
        old_utc = "2026-08-05T10:00:00.000000+00:00"

        _write_json(
            os.path.join(str(tmp_path), "paper_orders.json"),
            {"orders": [
                _order("BUY", early_wib, 0.0),
                _order("SELL", early_wib, 25.0),
                _order("BUY", mid_wib, 0.0),
                _order("SELL", mid_wib, 10.0),
                _order("BUY", old_utc, 0.0),
                _order("SELL", old_utc, -5.0),
            ]},
        )

        with patch.object(mm_mod, "datetime", _FakeDatetime):
            mm = MetricsManager(data_dir=str(tmp_path))
            utc_trades = mm.today_trades()
            wib_trades = mm.trades_since_wib_midnight()
            utc_summary = mm.today_summary()
            wib_summary = mm.today_summary_wib()

        assert [t["exit_time"] for t in utc_trades] == [mid_wib]
        assert sorted(t["exit_time"] for t in wib_trades) == [early_wib, mid_wib]
        # UTC-based methods are unchanged (BUG-2: must not move their base).
        assert utc_summary["total_trades"] == 1
        assert utc_summary["pnl"] == 10.0
        # WIB-based methods pick up the 01:00 WIB trade.
        assert wib_summary["total_trades"] == 2
        assert wib_summary["wins"] == 2
        assert wib_summary["losses"] == 0
        assert wib_summary["pnl"] == 35.0


# ===========================================================================
#  3.3 — Crash recovery docs exist
# ===========================================================================


class TestCrashRecoveryDocs:

    def test_crash_recovery_doc_exists(self) -> None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        doc = os.path.join(root, "docs", "CRASH_RECOVERY_TEST.md")
        assert os.path.exists(doc), "docs/CRASH_RECOVERY_TEST.md must exist"

    def test_crash_recovery_doc_has_all_scenarios(self) -> None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        doc = os.path.join(root, "docs", "CRASH_RECOVERY_TEST.md")
        with open(doc) as f:
            content = f.read()

        required = [
            "Scenario 1",   # process kill mid-trade
            "Scenario 2",   # kill during TP sell
            "Scenario 3",   # internet disconnection
            "Scenario 4",   # watchdog restart after crash
            "Scenario 5",   # power loss
            "Scenario 6",   # heartbeat stale
        ]
        for s in required:
            assert s in content, f"Missing {s} from CRASH_RECOVERY_TEST.md"

    def test_crash_recovery_doc_has_checklist(self) -> None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        doc = os.path.join(root, "docs", "CRASH_RECOVERY_TEST.md")
        with open(doc) as f:
            content = f.read()
        assert "Checklist" in content
        assert "PAPER_MODE=false" in content


# ===========================================================================
#  3.4 — BackupScheduler is wired into main.py startup (BUG-3)
# ===========================================================================


class TestMainBackupWiring:

    def test_daemon_auto_starts_backup_scheduler(self) -> None:
        """BUG-3 regression: main.py must start BackupScheduler by itself,
        producing a backup archive without any manual /backup or --backup."""
        import glob
        import subprocess
        import sys

        root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        backups_dir = os.path.join(root, "backups")
        os.makedirs(backups_dir, exist_ok=True)
        for f in glob.glob(os.path.join(backups_dir, "*.zip")):
            os.remove(f)

        env = {**os.environ, "TEST_MODE": "true"}
        proc = subprocess.Popen(
            [sys.executable, "main.py"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
        )
        try:
            deadline = time.time() + 30
            backup_seen: str | None = None
            while time.time() < deadline and proc.poll() is None:
                zips = glob.glob(os.path.join(backups_dir, "*.zip"))
                for zp in zips:
                    if os.path.getsize(zp) > 0:
                        backup_seen = zp
                        break
                if backup_seen:
                    break
                time.sleep(0.5)
            assert backup_seen is not None, (
                "main.py must auto-create a backup on startup (BUG-3)"
            )
            assert os.path.getsize(backup_seen) > 0
        finally:
            if proc.poll() is None:
                with open(os.path.join(root, "data", ".shutdown_requested"), "w") as f:
                    f.write("shutdown test")
                try:
                    proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            else:
                proc.wait()
            try:
                os.remove(os.path.join(root, "data", ".shutdown_requested"))
            except OSError:
                pass
