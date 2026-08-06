"""
Professional pipeline logger for ZetBot AI.

Provides console output with stage markers and file logging with full
detail.  Each pipeline stage's stdout/stderr is captured and written
to the log file while the console shows a clean ``[HH:MM:SS]
Stage...DONE`` format.

Usage::

    from scripts.logger import PipelineLogger
    logger = PipelineLogger(config)
    logger.stage_start("Scanner")
    # ... do work ...
    logger.stage_done("Scanner", "420 pairs")
"""

import io
import os
import sys
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from scripts.app_config import AppConfig


class PipelineLogger:
    """Log pipeline stage execution to console and a structured log file.

    Log rotation:
        A new log file is created each UTC day (``YYYY-MM-DD.log``).
        On startup (and on each ``_write_log`` call when the date has
        rolled over) old log files beyond ``LOG_KEEP_DAYS`` are deleted
        automatically so the logs/ directory never grows unbounded.
    """

    LOG_KEEP_DAYS: int = 14  # keep the last N daily log files

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._ensure_log_dir()
        self._log_path = self._log_path_for_today()
        self._log_date: str = self._today_str()
        # Prune old logs at startup (non-fatal).
        try:
            self._prune_old_logs()
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def stage_start(self, name: str) -> None:
        """Log the beginning of a pipeline stage."""
        self._write_console(f"[{self._ts()}] {name}...", end="", flush=True)
        self._write_log(f"STAGE START  {name}")

    def stage_done(self, name: str, detail: str = "") -> None:
        """Log successful completion of a pipeline stage."""
        msg = "DONE"
        if detail:
            msg += f"  {detail}"
        self._write_console(msg)
        self._write_log(f"STAGE DONE   {name}  {detail}")

    def stage_fail(self, name: str, reason: str) -> None:
        """Log failure of a pipeline stage."""
        self._write_console("FAILED")
        self._write_console(f"  {reason}")
        self._write_log(f"STAGE FAIL   {name}  {reason}")

    def pipeline_start(self) -> None:
        """Log pipeline start."""
        self._write_console(f"[{self._ts()}] Pipeline started")
        self._write_log("PIPELINE START")

    def pipeline_end(self, total_elapsed: float) -> None:
        """Log pipeline end with total duration."""
        self._write_console(f"[{self._ts()}] Pipeline finished ({total_elapsed:.1f}s)")
        self._write_log(f"PIPELINE END  {total_elapsed:.1f}s")

    def summary(self, lines: list[str]) -> None:
        """Print a formatted summary block."""
        self._write_console("")
        self._write_console(f"  {'=' * 48}")
        self._write_console(f"  SUMMARY")
        self._write_console(f"  {'=' * 48}")
        for line in lines:
            self._write_console(f"  {line}")
        self._write_console(f"  {'=' * 48}")
        self._write_console("")
        for line in lines:
            self._write_log(f"SUMMARY  {line}")

    @contextmanager
    def capture_output(self) -> Iterator[None]:
        """Context manager that redirects stdout/stderr into the log file."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            out = stdout_buf.getvalue()
            err = stderr_buf.getvalue()
            if out.strip():
                for line in out.rstrip().split("\n"):
                    stripped = line.strip()
                    if stripped:
                        self._write_log(f"STDOUT  {stripped}")
            if err.strip():
                for line in err.rstrip().split("\n"):
                    stripped = line.strip()
                    if stripped:
                        self._write_log(f"STDERR  {stripped}")

    def info(self, message: str) -> None:
        """Log an informational message."""
        self._write_console(f"[{self._ts()}] {message}")
        self._write_log(f"INFO  {message}")

    def error(self, message: str) -> None:
        """Log an error message."""
        self._write_console(f"[{self._ts()}] ERROR  {message}")
        self._write_log(f"ERROR  {message}")

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self._write_console(f"[{self._ts()}] WARNING  {message}")
        self._write_log(f"WARNING  {message}")

    def debug(self, message: str) -> None:
        """Log a debug message."""
        self._write_log(f"DEBUG  {message}")

    def critical(self, message: str) -> None:
        """Log a critical message."""
        self._write_console(f"[{self._ts()}] CRITICAL  {message}")
        self._write_log(f"CRITICAL  {message}")

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def _ensure_log_dir(self) -> None:
        os.makedirs(self.config.logs_dir, exist_ok=True)

    @staticmethod
    def _today_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _log_path_for_today(self) -> str:
        return os.path.join(self.config.logs_dir, f"{self._today_str()}.log")

    def _maybe_rotate(self) -> None:
        """Roll over to a new log file when the UTC date changes."""
        today = self._today_str()
        if today != self._log_date:
            self._log_date = today
            self._log_path = self._log_path_for_today()
            try:
                self._prune_old_logs()
            except Exception:
                pass

    def _prune_old_logs(self) -> None:
        """Delete log files older than LOG_KEEP_DAYS days."""
        logs_dir = self.config.logs_dir
        if not os.path.isdir(logs_dir):
            return
        import time as _t
        cutoff = _t.time() - self.LOG_KEEP_DAYS * 86400
        for fname in os.listdir(logs_dir):
            if not fname.endswith(".log"):
                continue
            fpath = os.path.join(logs_dir, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
            except OSError:
                pass

    @staticmethod
    def _write_console(msg: str, **kwargs: Any) -> None:
        kwargs.setdefault("flush", True)
        print(msg, **kwargs)

    def _write_log(self, msg: str) -> None:
        self._maybe_rotate()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] {msg}\n")
