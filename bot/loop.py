"""
Trading Loop Engine

Continuous trading loop that executes PaperTradingEngine cycles
automatically until interrupted.

ZetBot AI
"""

import logging
import signal
import threading
import time
from datetime import date, datetime
from typing import Any

from bot.config import CONFIG
from bot.paper_engine import PaperTradingEngine

logger = logging.getLogger("ZetBot")


class TradingLoop:
    """Continuous trading loop for automated paper trading.

    Runs ``PaperTradingEngine`` cycles in a loop with configurable
    interval, retry logic, and graceful shutdown.

    Usage::

        loop = TradingLoop()
        loop.run()
    """

    def __init__(
        self,
        engine: PaperTradingEngine | None = None,
        interval: int | None = None,
    ) -> None:
        self._engine: PaperTradingEngine = engine or PaperTradingEngine()
        self._running: bool = False
        self._cycle_count: int = 0
        self._interval: int = (
            interval if interval is not None
            else int(CONFIG.get("loop_interval_seconds", 60))
        )
        self._max_retry: int = int(CONFIG.get("max_retry", 3))
        self._retry_delay: int = int(CONFIG.get("retry_delay", 5))
        self._symbol: str = str(CONFIG.get("symbol", "BTC/USDT"))
        self._timeframe: str = str(CONFIG.get("timeframe", "1h"))
        self._exchange: str = str(CONFIG.get("exchange", "binance"))
        self._stop_requested: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._signal_installed: bool = False
        self._last_summary_date: date | None = None

        # Install SIGINT handler for graceful shutdown (main thread only)
        if threading.current_thread() is threading.main_thread():
            self._original_sigint: Any = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_signal)
            self._signal_installed = True

        logger.info(
            "TradingLoop initialised — interval=%ds max_retry=%d %s %s",
            self._interval, self._max_retry, self._symbol, self._timeframe,
        )

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the trading loop continuously until interrupted.

        Each iteration::

            1. Fetch latest OHLCV
            2. Update indicators
            3. Evaluate strategy
            4. Manage current paper position
            5. Open paper BUY if signal exists
            6. Monitor TP / SL
            7. Close position if needed
            8. Update statistics
            9. Log result


        The loop sleeps for ``loop_interval_seconds`` between cycles.
        """
        if self._running:
            logger.warning("TradingLoop is already running — ignoring duplicate start")
            return

        self._running = True
        self._stop_requested = False
        logger.info("TradingLoop started")
        notifier = getattr(self._engine, "_notifier", None)
        if notifier:
            notifier.bot_started(
                symbol=self._symbol,
                timeframe=self._timeframe,
                exchange=self._exchange,
            )

        # Restore persistent state if available
        restored = self._engine.restore_state()
        if restored:
            logger.info("Previous state restored — resuming monitoring")

        try:
            while self._running:
                self._cycle_count += 1
                cycle_start = time.time()

                try:
                    self._execute_cycle()
                except KeyboardInterrupt:
                    logger.info("KeyboardInterrupt received — shutting down")
                    self._running = False
                    break
                except Exception as exc:
                    logger.error("Cycle %d failed: %s", self._cycle_count, exc)
                    self._handle_error(exc)
                    if not self._running:
                        break

                elapsed = time.time() - cycle_start
                logger.info(
                    "Cycle %d complete — %.2fs elapsed",
                    self._cycle_count, elapsed,
                )

                self._maybe_send_daily_summary()

                if self._running:
                    self._sleep()
        finally:
            self._save_state_on_shutdown()
            self._restore_signal_handler()
            self._running = False

        logger.info(
            "TradingLoop stopped — cycles=%d", self._cycle_count,
        )
        notifier = getattr(self._engine, "_notifier", None)
        if notifier:
            notifier.bot_stopped(
                cycles=self._cycle_count,
                balance=self._engine.current_balance(),
            )

    def stop(self) -> None:
        """Request the loop to stop after the current cycle."""
        logger.info("Stop requested — finishing current cycle")
        self._running = False
        self._stop_event.set()

    @property
    def cycle_count(self) -> int:
        """Number of completed cycles."""
        return self._cycle_count

    @property
    def is_running(self) -> bool:
        """Whether the loop is currently running."""
        return self._running

    @property
    def engine(self) -> PaperTradingEngine:
        """The underlying paper trading engine."""
        return self._engine

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _save_state_on_shutdown(self) -> None:
        """Persist engine state during shutdown so the next start can
        recover without data loss."""
        try:
            self._engine._save_auto_state()
            logger.debug("State saved on shutdown")
        except Exception:
            logger.exception("Failed to save state on shutdown")

    def _execute_cycle(self) -> None:
        """Execute one complete paper trading cycle."""
        result = self._engine.run_once(symbol=self._symbol, timeframe=self._timeframe)
        self._log_cycle(result)

    def _log_cycle(self, result: dict[str, Any]) -> None:
        """Log the result of one trading cycle.

        Fields logged: timestamp, exchange, pair, price, EMA200, RSI,
        ADX, ATR, market state, signal, position, balance, PnL, reasons,
        cycle execution time.
        """
        signal = result.get("signal") or {}
        trade = result.get("trade")
        position = result.get("position")
        price = result.get("price", 0.0)
        market_state = result.get("market_state", "?")
        balance = self._engine.current_balance()

        signal_name = signal.get("signal", "?") if signal else "?"
        reasons = signal.get("reason", []) if signal else []
        pnl = trade["net_pnl"] if trade else 0.0

        logger.info(
            "Cycle %d | %s %s | Price=%.2f | Market=%s | "
            "Signal=%s | Position=%s | Balance=%.2f | PnL=%+.2f | "
            "Reasons=%s",
            self._cycle_count,
            self._exchange, self._symbol,
            price,
            market_state,
            signal_name,
            "YES" if position else "NO",
            balance,
            pnl,
            " | ".join(reasons) if reasons else "—",
        )

    def _sleep(self) -> None:
        """Sleep for the configured interval.

        Uses a threading.Event so that :meth:`stop` can interrupt
        the sleep immediately.
        """
        if self._interval > 0:
            logger.debug("Sleeping %ds until next cycle", self._interval)
            self._stop_event.wait(timeout=self._interval)
            self._stop_event.clear()

    def _handle_error(self, exc: Exception) -> None:
        """Handle a cycle error with retry logic.

        Retries up to ``max_retry`` times with ``retry_delay``
        seconds between attempts.
        """
        for attempt in range(1, self._max_retry + 1):
            if not self._running:
                return
            logger.warning(
                "Retry %d/%d after error: %s", attempt, self._max_retry, exc,
            )
            try:
                self._stop_event.wait(timeout=self._retry_delay)
                self._stop_event.clear()
                if not self._running:
                    return
                self._execute_cycle()
                logger.info("Retry %d succeeded", attempt)
                return
            except Exception as nested:
                logger.error("Retry %d failed: %s", attempt, nested)

        logger.error(
            "All %d retries exhausted — skipping cycle %d",
            self._max_retry, self._cycle_count,
        )
        notifier = getattr(self._engine, "_notifier", None)
        if notifier:
            notifier.error_occurred(
                f"Cycle {self._cycle_count} failed after {self._max_retry} retries: {exc}",
            )

    def _maybe_send_daily_summary(self) -> None:
        """Send a daily summary if the day has changed."""
        today = date.today()
        if self._last_summary_date == today:
            return
        self._last_summary_date = today
        notifier = getattr(self._engine, "_notifier", None)
        if notifier:
            stats = self._engine.statistics()
            notifier.daily_summary(stats, self._engine.current_balance())

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        """Signal handler for graceful shutdown."""
        logger.info("Signal %d received — stopping loop", signum)
        self.stop()
        self._restore_signal_handler()

    def _restore_signal_handler(self) -> None:
        """Restore the original SIGINT handler (main thread only)."""
        if self._signal_installed and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._original_sigint)
