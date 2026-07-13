"""Background scheduler for LIVE protection-order reconciliation.

Separate from ``PipelineScheduler`` (which runs the scan/decision
pipeline on a much longer interval, e.g. minutes) because the
synthetic-OCO gap disclosed in ``scripts/protection_manager.py`` only
stays small if reconciliation runs OFTEN — every few seconds, not
every few minutes. Reusing PipelineScheduler's loop wasn't a good fit
either: it assumes a list of pipeline-stage results with a ``.success``
attribute, not the dict of {symbol: protection_record} that
``reconcile_all_protections()`` returns.

Only meaningful in LIVE mode. Each tick checks ``order_manager.mode``
itself and skips entirely (not even one API call) when PAPER — so
injecting this unconditionally is harmless, but main.py only starts it
when already in LIVE mode to avoid running an idle thread for nothing.
"""

import logging
import threading
import time
from typing import Any, Optional


class ProtectionScheduler:
    """Background thread that calls
    ``order_manager.reconcile_all_protections()`` at a fixed interval.

    Usage::

        scheduler = ProtectionScheduler(
            order_manager=container.order, interval=8.0, logger=logger,
        )
        scheduler.start()
        ...
        scheduler.stop()
    """

    def __init__(
        self,
        order_manager: Any,
        interval: float = 8.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._order = order_manager
        self._interval = max(1.0, interval)
        self._log = logger or logging.getLogger("ZetBot")

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        self._run_count = 0
        self._last_status = "idle"

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                self._log.warning("ProtectionScheduler already running")
                return
            self._running = True

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ProtectionScheduler",
        )
        self._thread.start()
        self._log.info(
            f"Protection reconciliation scheduler started "
            f"(every {self._interval:.0f}s, LIVE mode only)."
        )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "run_count": self._run_count,
                "last_status": self._last_status,
            }

    # ------------------------------------------------------------------
    #  Loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        # Check the running flag every 0.1s (not just once per interval)
        # so stop() responds quickly instead of waiting a full interval.
        ticks_per_interval = max(1, int(self._interval * 10))
        while True:
            for _ in range(ticks_per_interval):
                with self._lock:
                    if not self._running:
                        return
                time.sleep(0.1)
            self._tick()

    def _tick(self) -> None:
        try:
            if getattr(self._order, "mode", None) != "LIVE":
                return  # PAPER — skip entirely, not even one API call

            results = self._order.reconcile_all_protections()

            with self._lock:
                self._run_count += 1
                self._last_status = "ok"

            for symbol, record in (results or {}).items():
                status = record.get("status") if record else "UNKNOWN"
                if status in ("STOP_FILLED", "TP_FILLED"):
                    self._log.warning(
                        f"Protection resolved for {symbol}: {status} "
                        "(sibling order cancelled)."
                    )
                elif status == "ERROR":
                    self._log.error(
                        f"Protection reconciliation ERROR for {symbol}: "
                        f"{record.get('error')}"
                    )
        except Exception as exc:
            with self._lock:
                self._last_status = f"failed: {exc}"
            self._log.error(f"Protection reconciliation tick failed: {exc}")
