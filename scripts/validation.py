"""
Long Paper Trading Validation Script

Runs hundreds of simulated paper trades through the unmodified
PaperTradingEngine and collects comprehensive statistics.

Usage::

    python -m scripts.validation

Output: ``data/validation_trades.csv``
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

# Disable logging noise during validation
logging.getLogger("ZetBot").setLevel(logging.WARNING)

from bot.config import CONFIG
from bot.paper_engine import PaperTradingEngine

# ---------------------------------------------------------------------------
#  Config overrides — no production code changed
# ---------------------------------------------------------------------------

CONFIG["auto_save"] = False
CONFIG["telegram_enabled"] = False

# ---------------------------------------------------------------------------
#  Synthetic market scenarios (same logic as test helpers)
# ---------------------------------------------------------------------------


def _uptrend_buy_df() -> pd.DataFrame:
    """Strong uptrend with a dip — triggers BUY signal."""
    n = 250
    highs, lows, close = [], [], []
    for i in range(n):
        if i < 220:
            c = 50_000.0 + i * 30.0
        else:
            c = 50_000.0 + 220 * 30.0 - (i - 220) * 25.0
        close.append(c)
        highs.append(c + 200.0)
        lows.append(c - 200.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _tp_hit_df(entry: float) -> pd.DataFrame:
    """Price above TP level."""
    return pd.DataFrame({
        "high": [entry * 1.05] * 250,
        "low": [entry * 0.95] * 250,
        "close": [entry * 1.04] * 250,
    })


def _sl_hit_df(entry: float) -> pd.DataFrame:
    """Price below SL level."""
    return pd.DataFrame({
        "high": [entry * 1.01] * 250,
        "low": [entry * 0.96] * 250,
        "close": [entry * 0.98] * 250,
    })


def _strategy_exit_df(entry: float, stop_loss_price: float) -> pd.DataFrame:
    """Price below EMA200 but above SL — triggers Strategy Exit.

    Entry ~55 845 → SL ~55 007.  The series starts at 55 500 and
    declines steadily to 55 200 (well above SL), while the lagging
    EMA200 stays above the final price → SELL signal.
    """
    n = 250
    start = 55_500.0
    end = 55_200.0
    step = (start - end) / (n - 1)
    close = [start - i * step for i in range(n)]
    return pd.DataFrame({
        "high": [c + 100.0 for c in close],
        "low": [c - 100.0 for c in close],
        "close": close,
    })


# ---------------------------------------------------------------------------
#  Validation runner
# ---------------------------------------------------------------------------


def _exit_reason_tag(reason: str) -> str:
    mapping = {
        "Take Profit": "TP",
        "Stop Loss": "SL",
        "Strategy Exit": "EXIT",
    }
    return mapping.get(reason, reason)


def _compute_stats(trades: list[dict]) -> dict[str, Any]:
    total = len(trades)
    if total == 0:
        return {"total_trades": 0}

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    tp_trades = [t for t in trades if t["exit_reason"] == "Take Profit"]
    sl_trades = [t for t in trades if t["exit_reason"] == "Stop Loss"]
    exit_trades = [t for t in trades if t["exit_reason"] == "Strategy Exit"]
    total_profit = sum(t["net_pnl"] for t in trades)
    win_total = sum(t["net_pnl"] for t in wins)
    loss_total = sum(t["net_pnl"] for t in losses)

    holding_seconds = [
        t["holding_time"].total_seconds()
        for t in trades
        if hasattr(t["holding_time"], "total_seconds")
    ]
    avg_hold = sum(holding_seconds) / len(holding_seconds) if holding_seconds else 0.0

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / total * 100.0) if total else 0.0,
        "total_profit": sum(t["net_pnl"] for t in wins),
        "total_loss": sum(t["net_pnl"] for t in losses),
        "net_profit": total_profit,
        "average_win": win_total / len(wins) if wins else 0.0,
        "average_loss": loss_total / len(losses) if losses else 0.0,
        "largest_win": max(t["net_pnl"] for t in wins) if wins else 0.0,
        "largest_loss": min(t["net_pnl"] for t in losses) if losses else 0.0,
        "average_holding_seconds": avg_hold,
        "tp_count": len(tp_trades),
        "sl_count": len(sl_trades),
        "exit_count": len(exit_trades),
    }


def _save_csv(trades: list[dict], path: str) -> None:
    fieldnames = [
        "entry_time", "exit_time", "entry_price", "exit_price",
        "quantity", "position_size_percent",
        "stop_loss_price", "take_profit_price",
        "gross_pnl", "net_pnl", "pnl_pct",
        "holding_time_seconds", "exit_reason",
        "balance_after", "symbol", "timeframe",
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            holding = t.get("holding_time")
            if hasattr(holding, "total_seconds"):
                holding_secs = holding.total_seconds()
            else:
                holding_secs = 0.0
            row = {
                "entry_time": (
                    t["entry_time"].isoformat()
                    if hasattr(t["entry_time"], "isoformat")
                    else str(t["entry_time"])
                ),
                "exit_time": (
                    t["exit_time"].isoformat()
                    if hasattr(t["exit_time"], "isoformat")
                    else str(t["exit_time"])
                ),
                "entry_price": f"{t['entry_price']:.2f}",
                "exit_price": f"{t['exit_price']:.2f}",
                "quantity": f"{t['quantity']:.6f}",
                "position_size_percent": f"{t['position_size_percent']:.2f}",
                "stop_loss_price": f"{t['stop_loss_price']:.2f}",
                "take_profit_price": f"{t['take_profit_price']:.2f}",
                "gross_pnl": f"{t['gross_pnl']:.2f}",
                "net_pnl": f"{t['net_pnl']:.2f}",
                "pnl_pct": f"{t['pnl_pct']:.2f}",
                "holding_time_seconds": f"{holding_secs:.4f}",
                "exit_reason": t["exit_reason"],
                "balance_after": f"{t['balance_after']:.2f}",
                "symbol": t.get("symbol", ""),
                "timeframe": t.get("timeframe", ""),
            }
            writer.writerow(row)


def _print_stats(stats: dict) -> None:
    print()
    print("=" * 55)
    print("  PAPER TRADING VALIDATION RESULTS")
    print("=" * 55)
    print(f"  Total Trades       : {stats['total_trades']}")
    print(f"  Wins               : {stats['wins']}")
    print(f"  Losses             : {stats['losses']}")
    print(f"  Win Rate           : {stats['win_rate']:.1f}%")
    print(f"  Total Profit       : ${stats['total_profit']:+,.2f}")
    print(f"  Total Loss         : ${stats['total_loss']:+,.2f}")
    print(f"  Net Profit         : ${stats['net_profit']:+,.2f}")
    print(f"  Average Win        : ${stats['average_win']:+,.2f}")
    print(f"  Average Loss       : ${stats['average_loss']:+,.2f}")
    print(f"  Largest Win        : ${stats['largest_win']:+,.2f}")
    print(f"  Largest Loss       : ${stats['largest_loss']:+,.2f}")
    print(f"  Average Hold       : {_fmt_seconds(stats['average_holding_seconds'])}")
    print(f"  Take Profit        : {stats['tp_count']}")
    print(f"  Stop Loss          : {stats['sl_count']}")
    print(f"  Strategy Exit      : {stats['exit_count']}")
    print("=" * 55)
    print()


def _fmt_seconds(secs: float) -> str:
    if secs < 1.0:
        return f"{secs*1000:.0f}ms"
    h, r = divmod(int(secs), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _run_scenario(
    engine: PaperTradingEngine,
    exit_scenario: str,
) -> dict | None:
    """Run one trade cycle: BUY → wait → exit.

    Returns the closed trade dict, or None if no trade occurred.
    """
    # Phase 1 — BUY
    buy_df = _uptrend_buy_df()
    result = engine.run_once(df=buy_df)
    if result["position"] is None:
        return None

    pos = engine.current_position()
    if pos is None:
        return None

    entry = pos["entry_price"]
    sl = pos["stop_loss_price"]

    # Phase 2 — Exit
    time.sleep(0.002)   # ensure exit_time > entry_time

    if exit_scenario == "TP":
        exit_df = _tp_hit_df(entry)
    elif exit_scenario == "SL":
        exit_df = _sl_hit_df(entry)
    elif exit_scenario == "EXIT":
        exit_df = _strategy_exit_df(entry, sl)
    else:
        return None

    result = engine.run_once(df=exit_df)
    return result.get("trade")


def main() -> None:
    print("=" * 55)
    print("  ZETBOT AI — PAPER TRADING VALIDATION")
    print("=" * 55)
    print(f"  Started at : {datetime.now(timezone.utc).isoformat()}")
    print()

    engine = PaperTradingEngine(initial_balance=10_000.0)

    # Scenario weights: 40% TP, 30% SL, 30% Strategy Exit
    scenarios = (["TP"] * 120) + (["SL"] * 90) + (["EXIT"] * 90)

    trades: list[dict] = []
    total = len(scenarios)
    task_size = max(total // 20, 1)

    print(f"  Running {total} trades...")
    print()

    for i, scenario in enumerate(scenarios, 1):
        trade = _run_scenario(engine, scenario)
        if trade is not None:
            trades.append(trade)

        if i % task_size == 0 or i == total:
            pct = i * 100 // total
            tag = _exit_reason_tag(scenario)
            sys.stdout.write(
                f"\r  [{pct:3d}%] trade {i:3d}/{total} "
                f"({tag:4s}) — {len(trades)} completed"
            )
            sys.stdout.flush()

    print()
    print(f"  Completed trades : {len(trades)}")
    stats = _compute_stats(trades)

    csv_path = "data/validation_trades.csv"
    _save_csv(trades, csv_path)
    print(f"  CSV exported     : {csv_path}")

    _print_stats(stats)

    print(f"  Finished at : {datetime.now(timezone.utc).isoformat()}")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
