"""Automatic periodic pipeline scheduler.

Schedules pipeline execution at a configurable interval in a background
thread.  Prevents overlapping runs, survives Telegram / exchange failures,
and exposes status for monitoring.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional


class PipelineScheduler:
    """Background scheduler that runs the pipeline at a fixed interval.

    Usage::

        scheduler = PipelineScheduler(
            pipeline_fn=container.run_pipeline,
            interval=300,
            logger=logger,
        )
        scheduler.start()
        ...
        scheduler.stop()
    """

    def __init__(
        self,
        pipeline_fn: Callable[[], list[Any]],
        interval: float = 300.0,
        logger: Optional[logging.Logger] = None,
        shutdown_event: Optional[threading.Event] = None,
    ) -> None:
        self._pipeline_fn = pipeline_fn
        self._interval = interval
        self._log = logger or logging.getLogger("ZetBot")
        self._shutdown_event = shutdown_event

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._wake_event = threading.Event()

        # Scheduler state
        self._run_count: int = 0
        self._last_start: Optional[float] = None
        self._last_end: Optional[float] = None
        self._next_run: Optional[float] = None
        self._pipeline_busy = False
        self._last_status: str = "idle"

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler background thread."""
        with self._lock:
            if self._running:
                self._log.warning("PipelineScheduler already running")
                return
            self._running = True
            self._next_run = time.time() + self._interval
            self._wake_event.clear()

        # Wait for old thread to fully exit before creating a new one
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PipelineScheduler")
        self._thread.start()
        self._log.info(
            f"Scheduler started. Next pipeline in {int(self._interval)} seconds."
        )

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread."""
        with self._lock:
            self._running = False
            self._next_run = None

        self._wake_event.set()
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._log.warning("PipelineScheduler thread did not exit cleanly")

    def force_run(self) -> bool:
        """Trigger an immediate pipeline run (no-op if already running).

        Returns True if the run was triggered, False if skipped.
        """
        with self._lock:
            if self._pipeline_busy:
                self._log.warning("Pipeline already running — skipping force_run")
                return False
            self._pipeline_busy = True
            self._run_count += 1
            self._last_start = time.time()
            self._last_status = "running"

        self._log.info("Force pipeline run requested")
        try:
            results = self._pipeline_fn()
            with self._lock:
                self._last_status = "completed"
            self._log.info(
                f"Pipeline #{self._run_count} completed "
                f"({len(results)} stages, "
                f"{sum(1 for r in results if r.success)}/{len(results)} OK)."
            )
        except Exception as exc:
            with self._lock:
                self._last_status = f"failed: {exc}"
            self._log.error(f"Pipeline #{self._run_count} failed: {exc}")

        with self._lock:
            self._last_end = time.time()
            self._pipeline_busy = False
        return True

    # ------------------------------------------------------------------
    #  Status properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        with self._lock:
            if not self._running:
                return "stopped"
            if self._pipeline_busy:
                return "running"
            return self._last_status

    @property
    def run_count(self) -> int:
        with self._lock:
            return self._run_count

    @property
    def last_start(self) -> Optional[float]:
        with self._lock:
            return self._last_start

    @property
    def last_end(self) -> Optional[float]:
        with self._lock:
            return self._last_end

    @property
    def next_run(self) -> Optional[float]:
        with self._lock:
            if not self._running:
                return None
            return self._next_run

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while self._running:
            now = time.time()
            remaining = self._next_run - now if self._next_run else self._interval

            if remaining > 0:
                self._log.info(f"Sleeping... Next pipeline in {int(remaining)} seconds.")
                self._wake_event.wait(timeout=min(remaining, self._interval))
                self._wake_event.clear()
                if not self._running:
                    break
                now = time.time()
                if self._next_run and now < self._next_run:
                    continue

            self._execute_pipeline()

    def _execute_pipeline(self) -> None:
        if not self._running:
            return

        with self._lock:
            if self._pipeline_busy:
                self._log.warning("Pipeline still running — skipping overlapping execution")
                self._last_status = "skipped (overlap)"
                self._next_run = time.time() + self._interval
                return
            self._pipeline_busy = True
            self._run_count += 1
            run_number = self._run_count
            self._last_start = time.time()
            self._last_status = "running"

        self._log.info(f"Pipeline #{run_number} started.")

        try:
            results = self._pipeline_fn()
            with self._lock:
                self._last_status = "completed"
            self._log.info(
                f"Pipeline #{run_number} completed "
                f"({len(results)} stages, "
                f"{sum(1 for r in results if r.success)}/{len(results)} OK)."
            )
        except Exception as exc:
            with self._lock:
                self._last_status = f"failed: {exc}"
            self._log.error(f"Pipeline #{run_number} failed: {exc}")

        with self._lock:
            self._last_end = time.time()
            self._pipeline_busy = False
            self._next_run = time.time() + self._interval
