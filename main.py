#!/usr/bin/env python3
"""
ZetBot AI — Application Orchestrator (v0.3.0)

Entry point for the full trading analysis pipeline.
Runs all analysis stages in sequence and exits.

Usage::

    python main.py

All configuration is read from environment variables / .env.
See ``scripts.app_config`` for the full list of options.
"""

import json
import os
import sys
import time
from typing import Any

from scripts.app_config import load_config, AppConfig
from scripts.logger import PipelineLogger
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
    """Run the full ZetBot AI analysis pipeline."""
    t0 = time.time()

    # Load configuration
    try:
        config = load_config()
    except Exception as exc:
        print(f"ERROR: Failed to load configuration — {exc}")
        sys.exit(1)

    # Initialize logger
    logger = PipelineLogger(config)
    logger.info(f"ZetBot AI — Pipeline v0.3.0")
    logger.info(f"Exchange : {config.exchange}")
    logger.info(f"Timeframe: {config.timeframe}")
    logger.info(f"Balance  : ${config.account_balance:>8,.2f}")
    logger.info(f"Mode     : {'PAPER' if config.paper_mode else 'LIVE'}")

    # Run pipeline
    pipeline = Pipeline(config, logger)
    results = pipeline.run()

    # Summary
    total_elapsed = time.time() - t0
    summary_lines = _build_summary(results, config)
    logger.summary(summary_lines)

    # Telegram notification
    if config.telegram_enabled:
        logger.info("Sending Telegram notification...")
        _send_telegram(config, summary_lines)

    # Exit code
    any_failed = any(not r.success for r in results)
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
