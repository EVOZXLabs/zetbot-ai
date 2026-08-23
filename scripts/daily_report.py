"""Daily report scheduler for ZetBot AI.

Sends an automated trading summary via Telegram every day at 00:00 WIB
(Asia/Jakarta, UTC+7).

Usage::

    from scripts.daily_report import DailyReportScheduler
    scheduler = DailyReportScheduler(notifier, data_dir="data")
    scheduler.start()        # starts the background thread
    ...
    scheduler.stop()         # call at shutdown
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

_WIB = timezone(timedelta(hours=7))
_log = logging.getLogger("ZetBot")


def _seconds_until_midnight_wib() -> float:
    """Seconds until the next 00:00 WIB."""
    now = datetime.now(_WIB)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return max(0.0, (tomorrow - now).total_seconds())


class DailyReportScheduler:
    """Background thread that sends a daily trading summary at 00:00 WIB.

    Args:
        notifier: ``bot.notifier.Notifier`` instance (or any object with
            ``notify_daily_summary(stats, balance)``).
        data_dir: Directory that contains ``paper_balance.json`` and
            ``paper_orders.json``.
        shutdown_event: Optional shared event; the thread stops when set.
    """

    def __init__(
        self,
        notifier: Any,
        data_dir: str = "data",
        shutdown_event: Optional[threading.Event] = None,
    ) -> None:
        self._notifier = notifier
        self._data_dir = data_dir
        self._shutdown = shutdown_event or threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="DailyReportScheduler",
            daemon=True,
        )
        self._thread.start()
        _log.info(
            "DailyReportScheduler started — daily report at 00:00 WIB"
        )

    def stop(self) -> None:
        self._running = False
        self._shutdown.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def send_now(self) -> None:
        """Send the daily report immediately (e.g. for manual trigger)."""
        try:
            stats, balance = self._gather_stats()
            self._notifier.notify_daily_summary(stats, balance)
            _log.info("Daily report sent")
        except Exception as exc:
            _log.warning("Daily report failed: %s", exc)

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            wait = _seconds_until_midnight_wib()
            _log.info(
                "DailyReportScheduler: next report in %.0f seconds "
                "(%.1f hours)", wait, wait / 3600,
            )
            # Sleep in 30-second increments so shutdown is responsive.
            elapsed = 0.0
            while elapsed < wait and self._running:
                self._shutdown.wait(timeout=min(30.0, wait - elapsed))
                if self._shutdown.is_set():
                    self._running = False
                    return
                elapsed += 30.0
            if self._running:
                self.send_now()

    def _gather_stats(self) -> tuple[dict[str, Any], float]:
        """Read today's trade stats from canonical data files."""
        import json

        # Balance
        balance = 0.0
        try:
            with open(os.path.join(self._data_dir, "paper_balance.json")) as f:
                pb = json.load(f)
            balance = pb.get("final_balance", 0.0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Today's trades via MetricsManager — WIB day boundary (00:00 WIB,
        # UTC+7), matching when this report actually fires. The UTC-based
        # today_summary() would drop every trade closed 00:00-07:00 WIB.
        try:
            from scripts.metrics_manager import MetricsManager  # noqa: PLC0415
            mm = MetricsManager(self._data_dir)
            # At 00:00 WIB the current day just started — the useful
            # window is the PREVIOUS full WIB day, not the current one.
            today = mm._summarize_trades(mm.trades_of_previous_wib_day())
            return {
                "total_trades": today.get("total_trades", 0),
                "win_count": today.get("wins", 0),
                "loss_count": today.get("losses", 0),
                "win_rate": today.get("win_rate", 0.0),
                "total_profit": today.get("pnl", 0.0),
                "profit_factor": 0.0,
            }, balance
        except Exception:
            return {"total_trades": 0}, balance
