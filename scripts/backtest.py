"""
Historical Backtest Module

Walk-forwards through real OHLCV data, feeding candles sequentially
into the unmodified PaperTradingEngine.

Holding times are calculated from candle timestamps, not wall-clock
time.  All production code under ``bot/`` is left untouched.

Usage::

    python -m scripts.backtest

Output: ``data/backtest_trades.csv``
"""

import csv
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd

logging.getLogger("ZetBot").setLevel(logging.WARNING)

from bot.config import CONFIG
from bot.data import MarketData, NORMALIZED_COLUMNS
from bot.paper_engine import PaperTradingEngine

# ---------------------------------------------------------------------------
#  Config overrides — no production files changed
# ---------------------------------------------------------------------------

CONFIG["auto_save"] = False
CONFIG["telegram_enabled"] = False

# ---------------------------------------------------------------------------
#  Parameters
# ---------------------------------------------------------------------------

EXCHANGE = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LIMIT = 1000            # per-page candle limit (Binance max)
MAX_CANDLES = 5000      # total candles to collect
INITIAL_BALANCE = 10_000.0
WARMUP = 250            # candles needed for EMA200 / RSI / ADX warm-up
PAGE_MS = 3600_000      # 1 hour in milliseconds (for since=- pagination)

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class WalkForwardTrade:
    """A single completed trade with candle-based timestamps."""
    entry_idx: int
    exit_idx: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    holding_candles: int
    holding_duration: timedelta
    entry_price: float
    exit_price: float
    quantity: float
    position_size_percent: float
    stop_loss_price: float
    take_profit_price: float
    gross_pnl: float
    net_pnl: float
    pnl_pct: float
    balance_after: float
    equity_after: float
    exit_reason: str
    symbol: str
    timeframe: str


@dataclass
class EquityPoint:
    """Equity snapshot at a given candle timestamp."""
    timestamp: datetime
    equity: float
    drawdown: float = 0.0
    drawdown_pct: float = 0.0


# ---------------------------------------------------------------------------
#  Data fetching (paginated)
# ---------------------------------------------------------------------------


def fetch_data(
    exchange_name: str = EXCHANGE,
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    page_size: int = LIMIT,
    max_candles: int = MAX_CANDLES,
) -> pd.DataFrame:
    """Fetch OHLCV candles from the exchange with pagination.

    Returns a DataFrame sorted by timestamp (oldest first) with columns
    ``timestamp``, ``open``, ``high``, ``low``, ``close``, ``volume``.
    """
    md = MarketData(exchange_name=exchange_name)
    first_page = md.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=page_size)

    exchange = md.exchange
    all_pages: list[pd.DataFrame] = [first_page]
    oldest_ts = int(first_page["timestamp"].iloc[0].timestamp() * 1000)
    collected = len(first_page)

    while collected < max_candles:
        page = exchange.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=page_size,
            since=oldest_ts - (page_size * PAGE_MS),
        )
        if not page:
            break
        df_page = pd.DataFrame(page, columns=NORMALIZED_COLUMNS)
        df_page["timestamp"] = pd.to_datetime(
            df_page["timestamp"], unit="ms", utc=True,
        )

        all_ts: set[Any] = set()
        for p in all_pages:
            all_ts.update(p["timestamp"])
        df_page = df_page[~df_page["timestamp"].isin(all_ts)]
        if df_page.empty:
            break

        all_pages.insert(0, df_page)
        collected += len(df_page)
        oldest_ts = int(df_page["timestamp"].iloc[0].timestamp() * 1000)

    full = pd.concat(all_pages, ignore_index=True)
    full.sort_values("timestamp", inplace=True)
    full.reset_index(drop=True, inplace=True)
    return full


# ---------------------------------------------------------------------------
#  Equity / drawdown
# ---------------------------------------------------------------------------


class DrawdownTracker:
    """Tracks peak equity and computes drawdown on every update."""

    def __init__(self, initial_equity: float) -> None:
        self._peak = initial_equity

    def update(self, equity: float) -> tuple[float, float]:
        """Return (drawdown_$, drawdown_%) for the given equity value."""
        if equity > self._peak:
            self._peak = equity
        dd = self._peak - equity
        dd_pct = (dd / self._peak * 100.0) if self._peak > 0 else 0.0
        return dd, dd_pct


# ---------------------------------------------------------------------------
#  Statistics
# ---------------------------------------------------------------------------


@dataclass
class BacktestReport:
    """Aggregated statistics from a completed backtest run."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_profit: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    win_loss_ratio: float = 0.0
    expectancy: float = 0.0
    average_trade_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    average_holding_candles: float = 0.0
    average_holding_seconds: float = 0.0
    exposure_pct: float = 0.0
    equity_peak: float = 0.0
    final_equity: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    tp_count: int = 0
    sl_count: int = 0    # NOTE: "SL" is short-hand for stop-loss; keep as-is
    exit_count: int = 0


def _compute_exposure(trades: list[WalkForwardTrade], total_candles: int) -> float:
    """Percentage of walk-forward candles that had an open position."""
    if total_candles == 0:
        return 0.0
    held: set[int] = set()
    for t in trades:
        for c in range(t.entry_idx, t.exit_idx + 1):
            held.add(c)
    return len(held) / total_candles * 100.0


def compute_report(
    trades: list[WalkForwardTrade],
    equity_curve: list[EquityPoint],
    total_candles: int,
    initial_balance: float,
) -> BacktestReport:
    """Aggregate trade statistics into a BacktestReport."""
    r = BacktestReport()
    r.total_trades = len(trades)
    r.final_equity = equity_curve[-1].equity if equity_curve else initial_balance

    if r.total_trades == 0:
        r.equity_peak = r.final_equity
        return r

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    r.wins = len(wins)
    r.losses = len(losses)
    r.win_rate = (r.wins / r.total_trades) * 100.0

    r.gross_profit = sum(t.net_pnl for t in wins)
    r.gross_loss = sum(t.net_pnl for t in losses)
    r.net_profit = r.gross_profit + r.gross_loss

    r.total_return_pct = (
        (r.final_equity - initial_balance) / initial_balance * 100.0
    )

    if r.gross_loss != 0:
        r.profit_factor = abs(r.gross_profit / r.gross_loss)
    elif r.gross_profit > 0:
        r.profit_factor = float("inf")

    r.average_win = r.gross_profit / r.wins if r.wins else 0.0
    r.average_loss = r.gross_loss / r.losses if r.losses else 0.0
    r.largest_win = max(t.net_pnl for t in wins) if wins else 0.0
    r.largest_loss = min(t.net_pnl for t in losses) if losses else 0.0
    r.win_loss_ratio = (
        abs(r.average_win / r.average_loss)
        if r.average_loss != 0 else 0.0
    )

    r.expectancy = (
        (r.win_rate / 100.0 * r.average_win)
        - ((1 - r.win_rate / 100.0) * abs(r.average_loss))
    )

    pnl_pcts = [t.pnl_pct for t in trades]
    r.average_trade_pct = sum(pnl_pcts) / len(pnl_pcts)
    r.best_trade_pct = max(pnl_pcts)
    r.worst_trade_pct = min(pnl_pcts)

    durations_s = [
        t.holding_duration.total_seconds() for t in trades
    ]
    r.average_holding_seconds = (
        sum(durations_s) / len(durations_s) if durations_s else 0.0
    )
    r.average_holding_candles = (
        sum(t.holding_candles for t in trades) / r.total_trades
    )

    r.exposure_pct = _compute_exposure(trades, total_candles)

    r.tp_count = sum(1 for t in trades if t.exit_reason == "Take Profit")
    r.sl_count = sum(1 for t in trades if t.exit_reason == "Stop Loss")
    r.exit_count = sum(1 for t in trades if t.exit_reason == "Strategy Exit")

    # Equity curve
    if equity_curve:
        r.equity_peak = max(eq.equity for eq in equity_curve)
        r.max_drawdown = max(eq.drawdown for eq in equity_curve)
        r.max_drawdown_pct = max(eq.drawdown_pct for eq in equity_curve)

    return r


# ---------------------------------------------------------------------------
#  Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_duration(secs: float) -> str:
    if secs < 1.0:
        return f"{secs*1000:.1f}ms"
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _print_header(title: str, width: int = 58) -> None:
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def _print_report(r: BacktestReport, initial_balance: float) -> None:
    """Print the aggregated backtest report."""
    _print_header("BACKTEST RESULTS")
    print(f"  Total Trades            : {r.total_trades}")
    print(f"  Wins                    : {r.wins}")
    print(f"  Losses                  : {r.losses}")
    print(f"  Win Rate                : {r.win_rate:.1f}%" if r.total_trades else "  Win Rate                : N/A")
    print(f"  Net Profit              : ${r.net_profit:+,.2f}")
    print(f"  Total Return            : {r.total_return_pct:+.2f}%")
    print(f"  Profit Factor           : {r.profit_factor if r.profit_factor != float('inf') else 'inf'}")
    print(f"  Gross Profit            : ${r.gross_profit:+,.2f}")
    print(f"  Gross Loss              : ${r.gross_loss:+,.2f}")
    print(f"  Average Win             : ${r.average_win:+,.2f}")
    print(f"  Average Loss            : ${r.average_loss:+,.2f}")
    print(f"  Largest Win             : ${r.largest_win:+,.2f}")
    print(f"  Largest Loss            : ${r.largest_loss:+,.2f}")
    print(f"  Win / Loss Ratio        : {r.win_loss_ratio:.2f}" if r.win_loss_ratio else "  Win / Loss Ratio        : N/A")
    print(f"  Expectancy              : ${r.expectancy:+,.2f}")
    print(f"  Average Trade           : {r.average_trade_pct:+.2f}%")
    print(f"  Best Trade              : {r.best_trade_pct:+.2f}%")
    print(f"  Worst Trade             : {r.worst_trade_pct:+.2f}%")
    print(f"  Average Hold Candles    : {r.average_holding_candles:.1f}")
    print(f"  Average Hold Time       : {_fmt_duration(r.average_holding_seconds)}")
    print(f"  Exposure                : {r.exposure_pct:.1f}%")
    print(f"  Equity Peak             : ${r.equity_peak:,.2f}")
    print(f"  Final Equity            : ${r.final_equity:,.2f}")
    print(f"  Max Drawdown            : ${r.max_drawdown:,.2f}  ({r.max_drawdown_pct:.2f}%)")
    print(f"  Take Profit             : {r.tp_count}")
    print(f"  Stop Loss               : {r.sl_count}")
    print(f"  Strategy Exit           : {r.exit_count}")
    print("=" * 58)

    if r.total_trades == 0:
        print()
        print("  No trades were generated.")
        print("  The strategy BUY conditions are very selective:")
        print("    Price > EMA200 + RSI < 30 + ADX >= 25 (TRENDING).")
        print("  Consider a smaller timeframe or more data.")
        print("=" * 58)


def _print_trade_summary(trades: list[WalkForwardTrade]) -> None:
    """Print a per-trade summary section."""
    if not trades:
        return
    _print_header("TRADE SUMMARY")
    for n, t in enumerate(trades, 1):
        print(f"  Trade #{n}")
        print(f"    Entry   : {t.entry_timestamp.isoformat()}  "
              f"@{t.entry_price:,.2f}")
        print(f"    Exit    : {t.exit_timestamp.isoformat()}  "
              f"@{t.exit_price:,.2f}")
        print(f"    Duration: {t.holding_candles} candles  "
              f"({_fmt_duration(t.holding_duration.total_seconds())})")
        print(f"    PnL     : ${t.net_pnl:+,.2f}  ({t.pnl_pct:+.2f}%)")
        print(f"    Reason  : {t.exit_reason}")
        print()


# ---------------------------------------------------------------------------
#  CSV export
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "entry_timestamp",
    "exit_timestamp",
    "holding_candles",
    "holding_duration",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "net_pnl",
    "pnl_percent",
    "balance_after",
    "equity_after",
    "exit_reason",
]


def save_csv(trades: list[WalkForwardTrade], path: str) -> None:
    """Write every completed trade to a CSV file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({
                "entry_timestamp": t.entry_timestamp.isoformat(),
                "exit_timestamp": t.exit_timestamp.isoformat(),
                "holding_candles": str(t.holding_candles),
                "holding_duration": f"{t.holding_duration.total_seconds():.4f}",
                "entry_price": f"{t.entry_price:.2f}",
                "exit_price": f"{t.exit_price:.2f}",
                "gross_pnl": f"{t.gross_pnl:.2f}",
                "net_pnl": f"{t.net_pnl:.2f}",
                "pnl_percent": f"{t.pnl_pct:.2f}",
                "balance_after": f"{t.balance_after:.2f}",
                "equity_after": f"{t.equity_after:.2f}",
                "exit_reason": t.exit_reason,
            })
    print(f"\n  CSV exported  : {path}")


# ---------------------------------------------------------------------------
#  Walk-forward runner
# ---------------------------------------------------------------------------


def run_walk_forward(
    df: pd.DataFrame,
    engine: PaperTradingEngine,
    warmup: int,
    symbol: str,
    timeframe: str,
) -> tuple[list[WalkForwardTrade], list[EquityPoint]]:
    """Walk forward through *df* one candle at a time.

    Returns (trades, equity_curve).
    """
    timestamps: pd.Series = df["timestamp"]
    closes: pd.Series = df["close"]
    total = len(df)
    trades: list[WalkForwardTrade] = []
    equity_curve: list[EquityPoint] = []
    dd_tracker = DrawdownTracker(float(INITIAL_BALANCE))

    entry_idx: int | None = None
    prev_had_position = False
    processed = total - warmup
    bar_width = 50

    for i in range(warmup, total):
        window = df.iloc[: i + 1]
        ts = timestamps.iloc[i]
        price = float(closes.iloc[i])

        result = engine.run_once(symbol=symbol, timeframe=timeframe, df=window)

        has_position = result["position"] is not None

        # Detect position opening
        if not prev_had_position and has_position:
            entry_idx = i

        # Detect position closing
        trade_raw = result.get("trade")
        if trade_raw is not None and entry_idx is not None:
            entry_ts = timestamps.iloc[entry_idx]
            exit_ts = ts
            holding_candles = i - entry_idx
            holding_duration = exit_ts - entry_ts

            wft = WalkForwardTrade(
                entry_idx=entry_idx,
                exit_idx=i,
                entry_timestamp=entry_ts,
                exit_timestamp=exit_ts,
                holding_candles=holding_candles,
                holding_duration=holding_duration,
                entry_price=float(trade_raw["entry_price"]),
                exit_price=float(trade_raw["exit_price"]),
                quantity=float(trade_raw["quantity"]),
                position_size_percent=float(trade_raw["position_size_percent"]),
                stop_loss_price=float(trade_raw["stop_loss_price"]),
                take_profit_price=float(trade_raw["take_profit_price"]),
                gross_pnl=float(trade_raw["gross_pnl"]),
                net_pnl=float(trade_raw["net_pnl"]),
                pnl_pct=float(trade_raw["pnl_pct"]),
                balance_after=float(trade_raw["balance_after"]),
                equity_after=0.0,           # filled below
                exit_reason=str(trade_raw["exit_reason"]),
                symbol=str(trade_raw.get("symbol", symbol)),
                timeframe=str(trade_raw.get("timeframe", timeframe)),
            )

            # Equity after close = balance
            wft.equity_after = wft.balance_after
            trades.append(wft)
            entry_idx = None

        # Equity snapshot
        pos = engine.current_position()
        cash = engine.current_balance()

        if pos is not None:
            unrealized = (price - float(pos["entry_price"])) * float(pos["quantity"])
            equity = cash + unrealized
        else:
            equity = cash

        dd_val, dd_pct = dd_tracker.update(equity)
        equity_curve.append(EquityPoint(
            timestamp=ts, equity=equity,
            drawdown=dd_val, drawdown_pct=dd_pct,
        ))

        prev_had_position = has_position

        # Progress bar
        step = i - warmup + 1
        if step % max(1, processed // bar_width) == 0 or step == processed:
            pct = step * 100 // processed
            filled = pct * bar_width // 100
            bar = "#" * filled + "-" * (bar_width - filled)
            sys.stdout.write(
                f"\r  [{bar}] {pct:3d}%  step {step:5d}/{processed}  "
                f"trades={len(trades):3d}  equity=${equity:>8.2f}"
            )
            sys.stdout.flush()

    return trades, equity_curve


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()

    print()
    print("=" * 58)
    print("  ZETBOT AI — HISTORICAL BACKTEST")
    print("=" * 58)
    print(f"  Exchange  : {EXCHANGE}")
    print(f"  Symbol    : {SYMBOL}")
    print(f"  Timeframe : {TIMEFRAME}")
    print(f"  Max       : {MAX_CANDLES} candles")
    print(f"  Warmup    : {WARMUP} candles")
    print(f"  Page size : {LIMIT}")
    print(f"  Balance   : ${INITIAL_BALANCE:,.2f}")
    print()

    # ── 1. Fetch data ────────────────────────────────────────────────
    print("  Fetching data … ", end="", flush=True)
    try:
        full_df = fetch_data()
    except Exception as exc:
        print(f"FAILED\n\n  Error: {exc}")
        sys.exit(1)

    total_candles = len(full_df)
    print(f"{total_candles} candles fetched")

    date_from = full_df["timestamp"].iloc[0]
    date_to = full_df["timestamp"].iloc[-1]
    print(f"  Range     : {date_from.date()} → {date_to.date()}")
    print(f"  Duration  : {(date_to - date_from).days} days")

    if total_candles < WARMUP:
        print(f"\n  Error: need at least {WARMUP} candles, got {total_candles}")
        sys.exit(1)

    # ── 2. Create engine ─────────────────────────────────────────────
    engine = PaperTradingEngine(initial_balance=INITIAL_BALANCE)

    # ── 3. Walk forward ──────────────────────────────────────────────
    processed = total_candles - WARMUP
    print()
    print(f"  Processing {processed} candles …")
    print()

    trades, equity_curve = run_walk_forward(
        df=full_df, engine=engine,
        warmup=WARMUP, symbol=SYMBOL, timeframe=TIMEFRAME,
    )

    elapsed = time.time() - t0
    print()
    print()
    print(f"  Elapsed   : {elapsed:.1f}s")

    # ── 4. Report ────────────────────────────────────────────────────
    report = compute_report(
        trades, equity_curve,
        total_candles=processed,
        initial_balance=INITIAL_BALANCE,
    )
    _print_report(report, INITIAL_BALANCE)
    _print_trade_summary(trades)

    # ── 5. CSV ────────────────────────────────────────────────────────
    csv_path = "data/backtest_trades.csv"
    save_csv(trades, csv_path)

    # ── 6. Footer ─────────────────────────────────────────────────────
    print(f"\n  Completed at : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Duration     : {elapsed:.1f}s")
    print("=" * 58)
    print()


if __name__ == "__main__":
    main()
