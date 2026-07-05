#!/usr/bin/env python3
"""
ZetBot AI — Application Orchestrator (v0.5.0)

Production-hardened long-running daemon.  Integrates the trading
analysis pipeline with the Telegram Command Center in a single
process.  Includes PID lock, health monitor, thread watchdog, and
graceful shutdown.

Usage::

    python main.py

All configuration is read from environment variables / .env.
See ``scripts.app_config`` for the full list of options.
"""

import atexit
import json
import os
import signal
import sys
import threading
import time
from typing import Any

from scripts.app_config import (
    AppConfig,
    ConfigError,
    load_config,
    validate_config,
)
from scripts.health import HealthMonitor
from scripts.logger import PipelineLogger
from scripts.pidfile import PidFile
from scripts.pipeline import Pipeline, StageResult


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _read_json(path: str) -> dict[str, Any]:
    """Read a JSON file, returning an empty dict on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _build_summary(
    results: list[StageResult],
    config: AppConfig,
) -> list[str]:
    """Build human-readable summary lines from pipeline output files."""
    lines: list[str] = []

    scanner_data = _read_json("data/scanner_results.json")
    decision_data = _read_json("data/decision_results.json")
    risk_data = _read_json("data/risk_results.json")
    trade_data = _read_json("data/trade_plan.json")
    position_data = _read_json("data/positions.json")
    paper_balance = _read_json("data/paper_balance.json")

    # Scanner
    scanned = scanner_data.get("total_pairs", 0)
    results_list = scanner_data.get("results", scanner_data.get("sorted", []))
    buy_candidates = sum(
        1 for s in results_list if isinstance(s, dict) and s.get("overall_score", 0) >= 70
    ) if isinstance(results_list, list) else 0
    lines.append(f"Pairs scanned       : {scanned}")
    lines.append(f"BUY candidates      : {buy_candidates}")

    # Decision engine
    decisions_list = decision_data.get("decisions", [])
    total_analyzed = len(decisions_list)
    strong_buys = sum(
        1 for d in decisions_list if d.get("recommendation") == "STRONG BUY"
    ) if isinstance(decisions_list, list) else 0
    good = sum(
        1 for d in decisions_list if d.get("recommendation") == "GOOD"
    ) if isinstance(decisions_list, list) else 0
    lines.append(f"STRONG BUY signals  : {strong_buys}  (of {total_analyzed} analyzed)")
    if good:
        lines.append(f"GOOD signals        : {good}")

    # Risk manager
    risk_results_list = risk_data.get("results", [])
    total_risk = len(risk_results_list)
    approved = sum(
        1 for r in risk_results_list if r.get("approval") == "APPROVED"
    ) if isinstance(risk_results_list, list) else 0
    rejected = sum(
        1 for r in risk_results_list if r.get("approval") == "REJECTED"
    ) if isinstance(risk_results_list, list) else 0
    lines.append(f"Risk evaluations    : {total_risk}")
    lines.append(f"Approved trades     : {approved}")
    if rejected:
        lines.append(f"Rejected trades     : {rejected}")

    # Trade plan
    ready_plans = trade_data.get("ready_count", 0)
    lines.append(f"Ready plans         : {ready_plans}")

    # Positions
    pos_list = position_data.get("positions", [])
    open_positions = sum(
        1 for p in pos_list if p.get("status") == "OPEN"
    ) if isinstance(pos_list, list) else 0
    closed_positions = sum(
        1 for p in pos_list if p.get("status") in ("CLOSED", "STOPPED", "TIMEOUT")
    ) if isinstance(pos_list, list) else 0
    lines.append(f"Open positions      : {open_positions}")
    lines.append(f"Closed positions    : {closed_positions}")

    # Paper trading
    balance = paper_balance.get("final_balance", 0.0)
    equity = paper_balance.get("final_equity", 0.0)
    realized = paper_balance.get("realized_pnl", 0.0)
    unrealized = paper_balance.get("unrealized_pnl", 0.0)
    net_pnl = paper_balance.get("net_pnl", 0.0)
    win_rate = paper_balance.get("win_rate", 0.0)
    total_trades = paper_balance.get("total_trades", 0)
    if total_trades > 0:
        lines.append(f"Win rate            : {win_rate:.1f}%")
    lines.append(f"USDT balance        : ${balance:>10,.2f}")
    lines.append(f"Equity              : ${equity:>10,.2f}")
    lines.append(f"Realized PnL        : ${realized:>+10,.2f}")
    lines.append(f"Unrealized PnL      : ${unrealized:>+10,.2f}")
    lines.append(f"Net PnL             : ${net_pnl:>+10,.2f}")

    # Stage execution times
    lines.append("")
    lines.append("Stage times:")
    for r in results:
        tag = "OK" if r.success else "FAIL"
        lines.append(f"  {r.name:>12s}  {tag}  {r.duration:.1f}s")
    total = sum(r.duration for r in results)
    lines.append(f"  {'TOTAL':>12s}       {total:.1f}s")

    return lines


def _thread_exists(name: str) -> bool:
    """Check if a thread with the given name is alive (zombie detection)."""
    for t in threading.enumerate():
        if t.name == name and t.is_alive():
            return True
    return False


def _start_worker(
    name: str,
    target: Any,
    logger: PipelineLogger,
) -> threading.Thread | None:
    """Start a daemon worker thread if no thread with *name* exists.

    Returns the thread on success, None if a zombie duplicate was found.
    """
    if _thread_exists(name):
        logger.error(
            "[WATCHDOG] Duplicate worker '%s' detected — not starting",
            name,
        )
        return None
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _send_telegram(config: AppConfig, lines: list[str]) -> None:
    """Send summary via Telegram if configured."""
    if not config.telegram_enabled:
        return
    try:
        from bot.telegram import TelegramNotifier
        import bot.config as bot_cfg

        bot_cfg.CONFIG.update({
            "telegram_enabled": True,
            "telegram_token": config.telegram_token,
            "telegram_chat_id": config.telegram_chat_id,
            "telegram_timeout": config.telegram_timeout,
            "telegram_retry": config.telegram_retry,
        })

        notifier = TelegramNotifier()
        msg = "\n".join(line for line in lines[:25])
        notifier._send(f"\U0001f4ca *Pipeline Report*\n{msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run ZetBot AI daemon with integrated Telegram Command Center."""
    t0 = time.time()

    # Load and validate configuration
    try:
        config = load_config()
        validate_config(config)
    except ConfigError as exc:
        print(f"ERROR: Configuration validation failed:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Failed to load configuration — {exc}", file=sys.stderr)
        sys.exit(1)

    # Initialize logger
    logger = PipelineLogger(config)
    logger.info(f"ZetBot AI — Daemon v0.5.0")
    logger.info(f"Exchange : {config.exchange}")
    logger.info(f"Timeframe: {config.timeframe}")
    logger.info(f"Balance  : ${config.account_balance:>8,.2f}")
    logger.info(f"Mode     : {'PAPER' if config.paper_mode else 'LIVE'}")

    # ------------------------------------------------------------------
    #  PID lock — prevent duplicate instances
    # ------------------------------------------------------------------

    pid_file = PidFile(os.path.join(config.data_dir, "zetbot.pid"))
    if not pid_file.acquire():
        logger.error(
            "Another ZetBot instance is already running "
            "(PID file: %s/zetbot.pid) — exiting",
            config.data_dir,
        )
        sys.exit(1)
    atexit.register(pid_file.release)
    logger.info("PID lock acquired")

    # ------------------------------------------------------------------
    #  Shutdown coordination
    # ------------------------------------------------------------------

    shutdown = threading.Event()

    def _shutdown_handler(signum: int, _frame: Any) -> None:
        logger.info(f"Signal {signum} received — shutting down")
        shutdown.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    # ------------------------------------------------------------------
    #  Run pipeline once on startup
    # ------------------------------------------------------------------

    pipeline = Pipeline(config, logger)
    last_pipeline_time: float = time.time()
    try:
        results = pipeline.run()
    except Exception as exc:
        logger.error(f"Pipeline failed: {exc}")
        results = []

    total_elapsed = time.time() - t0
    if results:
        summary_lines = _build_summary(results, config)
        logger.summary(summary_lines)

        if config.telegram_enabled:
            logger.info("Sending Telegram notification...")
            _send_telegram(config, summary_lines)

        any_failed = any(not r.success for r in results)
        if any_failed:
            logger.error("Pipeline had failures — see logs for details")
    else:
        logger.info("Pipeline did not produce results — skipping summary")

    # ------------------------------------------------------------------
    #  Telegram Command Center — background daemon thread
    # ------------------------------------------------------------------

    center: Any = None
    tg_thread: threading.Thread | None = None

    test_mode = os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes")
    has_creds = bool(config.telegram_token and config.telegram_chat_id)

    if test_mode or (config.telegram_enabled and has_creds):
        from scripts.telegram_commands import TelegramCommandCenter

        center = TelegramCommandCenter(config, test_mode=test_mode)
        tg_thread = _start_worker(
            "TelegramCmd",
            center.run,
            logger,
        )
        if tg_thread:
            logger.info("Telegram Command Center started (background thread)")
        else:
            logger.error("Failed to start Telegram Command Center")
    else:
        if config.telegram_enabled and not has_creds:
            logger.warning(
                "Telegram enabled but TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing"
                " — command center disabled"
            )
        else:
            logger.info("Telegram disabled — command center not started")

    # ------------------------------------------------------------------
    #  Health Monitor — background thread
    # ------------------------------------------------------------------

    health = HealthMonitor(logger, interval=60.0)
    health.start()
    logger.info("Health Monitor started (every 60s)")

    # ------------------------------------------------------------------
    #  Keep alive — monitor workers, health, shutdown
    # ------------------------------------------------------------------

    logger.info("ZetBot AI is running. Press Ctrl+C to stop.")

    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=10.0)
            if shutdown.is_set():
                break

            # -- Watchdog: monitor Telegram thread -----------------------
            if tg_thread is not None and not tg_thread.is_alive():
                logger.warning(
                    "[WATCHDOG] TelegramCmd worker stopped — restarting"
                )
                from scripts.telegram_commands import TelegramCommandCenter

                center = TelegramCommandCenter(config, test_mode=test_mode)
                new_thread = _start_worker(
                    "TelegramCmd",
                    center.run,
                    logger,
                )
                if new_thread:
                    tg_thread = new_thread
                    logger.info(
                        "[WATCHDOG] TelegramCmd restart successful"
                    )
                else:
                    logger.error(
                        "[WATCHDOG] TelegramCmd restart failed "
                        "(zombie detected)"
                    )
    except KeyboardInterrupt:
        shutdown.set()

    # ------------------------------------------------------------------
    #  Graceful shutdown
    # ------------------------------------------------------------------

    logger.info("Shutting down...")

    # Stop health monitor first
    health.stop()

    # Stop Telegram command center
    if center:
        logger.info("Stopping Telegram Command Center...")
        center.stop()
        if tg_thread and tg_thread.is_alive():
            tg_thread.join(timeout=5.0)
            if tg_thread.is_alive():
                logger.warning("Telegram thread did not stop within 5s")

    # Remove PID file
    pid_file.release()
    logger.info("PID lock released")

    logger.info("ZetBot AI stopped")


if __name__ == "__main__":
    main()
