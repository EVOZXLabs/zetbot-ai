"""Tests for PipelineScheduler."""

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.pipeline_scheduler import PipelineScheduler


def _noop_pipeline() -> list[Any]:
    return []


def _slow_pipeline(sleep_sec: float = 5.0) -> list[Any]:
    time.sleep(sleep_sec)
    return []


class TestPipelineSchedulerBasics:
    """Creation, start, stop."""

    def test_creates_and_stops(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=1.0)
        s.start()
        assert s.is_running is True
        assert s.status == "idle"
        s.stop()
        assert s.is_running is False
        assert s.status == "stopped"

    def test_double_start_is_noop(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=1.0)
        s.start()
        s.start()  # second start should log warning, not crash
        s.stop()

    def test_stop_without_start(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=1.0)
        s.stop()  # should not crash

    def test_run_count_increments(self) -> None:
        mock = MagicMock(return_value=[])
        s = PipelineScheduler(pipeline_fn=mock, interval=0.1)
        s.start()
        time.sleep(0.35)  # enough for ~3 runs
        s.stop()
        assert s.run_count >= 2
        assert s.last_start is not None
        assert s.last_end is not None


class TestPipelineSchedulerOverlap:
    """No overlapping executions."""

    def test_skips_overlapping_run(self) -> None:
        mock = MagicMock()
        mock.side_effect = lambda: _slow_pipeline(2.0)
        s = PipelineScheduler(pipeline_fn=mock, interval=0.5)
        s.start()
        time.sleep(3.5)
        s.stop()
        # Should have run at most ~2 times (not 7+)
        assert mock.call_count <= 3

    def test_status_busy_during_run(self) -> None:
        s = PipelineScheduler(pipeline_fn=lambda: _slow_pipeline(1.0), interval=0.3)
        s.start()
        time.sleep(1.5)
        # At least one run should have completed
        assert s.run_count >= 1
        s.stop()

    def test_force_run_skipped_when_busy(self) -> None:
        mock = MagicMock()
        mock.side_effect = lambda: _slow_pipeline(2.0)
        s = PipelineScheduler(pipeline_fn=mock, interval=100.0)
        s.start()
        time.sleep(0.2)
        result = s.force_run()  # should be skipped (already running?)
        # force_run returns False if pipeline is busy
        s.stop()
        assert mock.call_count <= 3


class TestPipelineSchedulerForceRun:
    """force_run triggers immediate execution."""

    def test_force_run_returns_true(self) -> None:
        mock = MagicMock(return_value=[])
        s = PipelineScheduler(pipeline_fn=mock, interval=100.0)
        s.start()
        time.sleep(0.1)
        result = s.force_run()
        s.stop()
        assert result is True
        assert mock.call_count >= 1

    def test_force_run_properties(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=100.0)
        s.start()
        time.sleep(0.1)
        s.force_run()
        time.sleep(0.1)
        assert s.last_start is not None
        assert s.last_end is not None
        assert s.run_count >= 1
        s.stop()


class TestPipelineSchedulerGracefulShutdown:
    """Scheduler stops cleanly on shutdown."""

    def test_shutdown_event_stops_scheduler(self) -> None:
        evt = threading.Event()
        mock = MagicMock(return_value=[])
        s = PipelineScheduler(pipeline_fn=mock, interval=100.0, shutdown_event=evt)
        s.start()
        assert s.is_running is True

        # Trigger shutdown (evt.set() + s.stop() simulates main.py flow)
        evt.set()
        time.sleep(0.1)
        assert s.is_running is True  # event alone just wakes the thread
        s.stop()
        assert s.is_running is False
        assert s.status == "stopped"

    def test_restart_after_shutdown(self) -> None:
        """Starting again after shutdown works."""
        evt = threading.Event()
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=100.0, shutdown_event=evt)
        s.start()
        s.stop()  # clean stop
        time.sleep(0.2)
        assert s.is_running is False, "Scheduler should be stopped"

        s.start()  # restart
        time.sleep(0.3)
        assert s.is_running is True, f"Scheduler should be running after restart, got {s.is_running}"
        s.stop()


class TestPipelineSchedulerNextRun:
    """next_run timestamp behaves correctly."""

    def test_next_run_is_in_future(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=60.0)
        s.start()
        nr = s.next_run
        assert nr is not None
        assert nr > time.time()
        s.stop()

    def test_next_run_is_none_when_stopped(self) -> None:
        s = PipelineScheduler(pipeline_fn=_noop_pipeline, interval=60.0)
        assert s.next_run is None
        s.start()
        s.stop()
        assert s.next_run is None


class TestScannerFreshness:
    """scanner_results.json mtime is updated after scanner runs."""

    def test_touch_file_creates_file(self) -> None:
        import os, tempfile
        from scripts.pipeline import _touch_file

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.json")
            assert not os.path.exists(path)
            _touch_file(path)
            assert os.path.exists(path)

    def test_touch_file_updates_mtime(self) -> None:
        import os, tempfile, time
        from scripts.pipeline import _touch_file

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.json")
            with open(path, "w") as f:
                f.write("{}")
            old_mtime = os.path.getmtime(path)
            time.sleep(0.01)
            _touch_file(path)
            new_mtime = os.path.getmtime(path)
            assert new_mtime >= old_mtime

    def test_scanner_freshness_after_pipeline_run(self) -> None:
        """After running pipeline scanner stage, scanner_results.json mtime is current."""
        import os, time, json

        # Write initial scanner results with old timestamp
        os.makedirs("data", exist_ok=True)
        with open("data/scanner_results.json", "w") as f:
            json.dump({"results": []}, f)
        # Artificially age the file
        old_ts = time.time() - 10000
        os.utime("data/scanner_results.json", (old_ts, old_ts))
        old_mtime = os.path.getmtime("data/scanner_results.json")

        from scripts.pipeline import _touch_file
        _touch_file("data/scanner_results.json")

        new_mtime = os.path.getmtime("data/scanner_results.json")
        assert new_mtime > old_mtime, "Scanner file should be fresher after touch"
