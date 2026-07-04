"""
Strategy Optimizer for ZetBot AI

Grid-searches strategy parameters by running walk-forward backtests on
real Binance data.  Uses multiprocessing for throughput.

Usage::

    python -m scripts.optimizer

Outputs::

    data/optimizer_results.csv       — every combination with metrics
    data/leaderboard.txt             — top-20 text report
    data/best_parameters.json        — best params by net profit
"""

import csv
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from itertools import product
from multiprocessing import Pool
from typing import Any

import pandas as pd

logging.getLogger("ZetBot").setLevel(logging.WARNING)

from bot.config import CONFIG
from bot.data import MarketData, NORMALIZED_COLUMNS
from bot.paper_engine import PaperTradingEngine
from bot.strategy import StrategyEngine
from bot.indicators import IndicatorEngine

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
INITIAL_BALANCE = 10_000.0
LIMIT = 1000
MAX_CANDLES = 2000
PAGE_MS = 3600_000

DEFAULT_WARMUP = 350

# Grid ranges
EMA_VALUES = [100, 150, 200, 300]
RSI_PERIOD_VALUES = [7, 14, 21]
RSI_OVERSOLD_VALUES = [25, 30, 35]
ADX_THRESHOLD_VALUES = [20, 25, 30]
TAKE_PROFIT_VALUES = [1.5, 2.0, 3.0, 4.0]
STOP_LOSS_VALUES = [1.0, 1.5, 2.0]

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class ParamSet:
    """One combination of strategy parameters."""
    ema_period: int = 200
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    adx_threshold: float = 25.0
    take_profit_pct: float = 2.5
    stop_loss_pct: float = 1.5


@dataclass
class WalkForwardTrade:
    entry_idx: int
    exit_idx: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    holding_candles: int
    holding_duration: timedelta
    entry_price: float
    exit_price: float
    quantity: float
    net_pnl: float
    pnl_pct: float
    balance_after: float
    exit_reason: str


@dataclass
class EquityPoint:
    timestamp: datetime
    equity: float
    drawdown: float = 0.0
    drawdown_pct: float = 0.0


@dataclass
class EvalResult:
    """Metrics for one parameter combination."""
    params: ParamSet
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
    expectancy: float = 0.0
    sharpe_ratio: float = 0.0
    average_trade_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    average_holding_candles: float = 0.0
    exposure_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    final_equity: float = 0.0
    equity_peak: float = 0.0
    tp_count: int = 0
    sl_count: int = 0
    exit_count: int = 0
    elapsed: float = 0.0

# ---------------------------------------------------------------------------
#  Data fetching
# ---------------------------------------------------------------------------


def fetch_data() -> pd.DataFrame:
    """Fetch OHLCV candles from Binance with pagination."""
    md = MarketData(exchange_name=EXCHANGE)
    first_page = md.fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)

    exchange = md.exchange
    all_pages: list[pd.DataFrame] = [first_page]
    oldest_ts = int(first_page["timestamp"].iloc[0].timestamp() * 1000)
    collected = len(first_page)

    while collected < MAX_CANDLES:
        page = exchange.fetch_ohlcv(
            symbol=SYMBOL, timeframe=TIMEFRAME,
            limit=LIMIT, since=oldest_ts - (LIMIT * PAGE_MS),
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
#  EMA monkey-patch factory
# ---------------------------------------------------------------------------

def _make_ema200(period: int):
    """Return a staticmethod-compatible replacement for
    ``IndicatorEngine.ema200`` that uses the given *period*."""
    def ema200(df: pd.DataFrame, column: str = "close") -> float:
        series = df[column]
        ema_series = IndicatorEngine.ema(series, period=period)
        return float(ema_series.iloc[-1])
    ema200.__name__ = "ema200"
    return staticmethod(ema200)


# ---------------------------------------------------------------------------
#  Walk-forward runner (silent — no progress bar for workers)
# ---------------------------------------------------------------------------


class DrawdownTracker:
    def __init__(self, initial_equity: float) -> None:
        self._peak = initial_equity

    def update(self, equity: float) -> tuple[float, float]:
        if equity > self._peak:
            self._peak = equity
        dd = self._peak - equity
        dd_pct = (dd / self._peak * 100.0) if self._peak > 0 else 0.0
        return dd, dd_pct


def _run_walk_forward(
    df: pd.DataFrame,
    engine: PaperTradingEngine,
    warmup: int,
    symbol: str,
    timeframe: str,
    initial_balance: float,
) -> tuple[list[WalkForwardTrade], list[EquityPoint]]:
    """Walk forward — returns (trades, equity_curve)."""
    timestamps: pd.Series = df["timestamp"]
    closes: pd.Series = df["close"]
    total = len(df)
    trades: list[WalkForwardTrade] = []
    equity_curve: list[EquityPoint] = []
    dd_tracker = DrawdownTracker(initial_balance)

    entry_idx: int | None = None
    prev_had_position = False

    for i in range(warmup, total):
        window = df.iloc[: i + 1]
        ts = timestamps.iloc[i]
        price = float(closes.iloc[i])

        result = engine.run_once(symbol=symbol, timeframe=timeframe, df=window)
        has_position = result["position"] is not None

        if not prev_had_position and has_position:
            entry_idx = i

        trade_raw = result.get("trade")
        if trade_raw is not None and entry_idx is not None:
            entry_ts = timestamps.iloc[entry_idx]
            trades.append(WalkForwardTrade(
                entry_idx=entry_idx,
                exit_idx=i,
                entry_timestamp=entry_ts,
                exit_timestamp=ts,
                holding_candles=i - entry_idx,
                holding_duration=ts - entry_ts,
                entry_price=float(trade_raw["entry_price"]),
                exit_price=float(trade_raw["exit_price"]),
                quantity=float(trade_raw["quantity"]),
                net_pnl=float(trade_raw["net_pnl"]),
                pnl_pct=float(trade_raw["pnl_pct"]),
                balance_after=float(trade_raw["balance_after"]),
                exit_reason=str(trade_raw["exit_reason"]),
            ))
            entry_idx = None

        pos = engine.current_position()
        cash = engine.current_balance()
        equity = (
            cash + (price - float(pos["entry_price"])) * float(pos["quantity"])
            if pos is not None else cash
        )
        dd_val, dd_pct = dd_tracker.update(equity)
        equity_curve.append(EquityPoint(ts, equity, dd_val, dd_pct))
        prev_had_position = has_position

    return trades, equity_curve


# ---------------------------------------------------------------------------
#  Metrics
# ---------------------------------------------------------------------------


def _compute_exposure(trades: list[WalkForwardTrade], total_candles: int) -> float:
    if not trades or total_candles == 0:
        return 0.0
    held: set[int] = set()
    for t in trades:
        for c in range(t.entry_idx, t.exit_idx + 1):
            held.add(c)
    return len(held) / total_candles * 100.0


def _sharpe_ratio(pnl_pcts: list[float]) -> float:
    """Compute an approximate Sharpe Ratio from per-trade returns.

    Formula:  Sharpe = (mean(PnL%) / std(PnL%)) * sqrt(trades)

    This is an **information ratio** for ranking — not an annualised
    financial Sharpe.
    """
    if len(pnl_pcts) < 2:
        return 0.0
    mean_pnl = sum(pnl_pcts) / len(pnl_pcts)
    variance = sum((p - mean_pnl) ** 2 for p in pnl_pcts) / (len(pnl_pcts) - 1)
    if variance <= 0:
        return 0.0
    std_pnl = math.sqrt(variance)
    return round((mean_pnl / std_pnl) * math.sqrt(len(pnl_pcts)), 4)


def compute_eval(
    params: ParamSet,
    trades: list[WalkForwardTrade],
    equity_curve: list[EquityPoint],
    total_candles: int,
    initial_balance: float,
) -> EvalResult:
    """Aggregate trade/equity data into an EvalResult."""
    r = EvalResult(params=params)
    r.total_trades = len(trades)
    r.final_equity = equity_curve[-1].equity if equity_curve else initial_balance
    r.equity_peak = max(eq.equity for eq in equity_curve) if equity_curve else initial_balance
    r.max_drawdown = max(eq.drawdown for eq in equity_curve) if equity_curve else 0.0
    r.max_drawdown_pct = max(eq.drawdown_pct for eq in equity_curve) if equity_curve else 0.0

    if r.total_trades == 0:
        r.final_equity = initial_balance
        return r

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    r.wins = len(wins)
    r.losses = len(losses)
    r.win_rate = r.wins / r.total_trades * 100.0

    r.gross_profit = sum(t.net_pnl for t in wins)
    r.gross_loss = sum(t.net_pnl for t in losses)
    r.net_profit = r.gross_profit + r.gross_loss
    r.total_return_pct = (r.final_equity - initial_balance) / initial_balance * 100.0

    if r.gross_loss != 0:
        r.profit_factor = abs(r.gross_profit / r.gross_loss)
    elif r.gross_profit > 0:
        r.profit_factor = float("inf")

    r.average_win = r.gross_profit / r.wins if r.wins else 0.0
    r.average_loss = r.gross_loss / r.losses if r.losses else 0.0
    r.largest_win = max(t.net_pnl for t in wins) if wins else 0.0
    r.largest_loss = min(t.net_pnl for t in losses) if losses else 0.0

    r.expectancy = (
        (r.win_rate / 100.0 * r.average_win)
        - ((1.0 - r.win_rate / 100.0) * abs(r.average_loss))
    )

    pnl_pcts = [t.pnl_pct for t in trades]
    r.average_trade_pct = sum(pnl_pcts) / len(pnl_pcts)
    r.best_trade_pct = max(pnl_pcts)
    r.worst_trade_pct = min(pnl_pcts)

    r.sharpe_ratio = _sharpe_ratio(pnl_pcts)

    r.average_holding_candles = (
        sum(t.holding_candles for t in trades) / r.total_trades
    )

    r.exposure_pct = _compute_exposure(trades, total_candles)

    r.tp_count = sum(1 for t in trades if t.exit_reason == "Take Profit")
    r.sl_count = sum(1 for t in trades if t.exit_reason == "Stop Loss")
    r.exit_count = sum(1 for t in trades if t.exit_reason == "Strategy Exit")

    return r


# ---------------------------------------------------------------------------
#  Single evaluation (runs in worker process)
# ---------------------------------------------------------------------------

# Global reference for the worker — set via Pool initializer
_WORKER_DF: pd.DataFrame | None = None


def _worker_init(df: pd.DataFrame) -> None:
    """Initialise each worker with the shared DataFrame."""
    global _WORKER_DF
    _WORKER_DF = df


def evaluate_one(params: ParamSet) -> EvalResult:
    """Evaluate one parameter set.  Called in worker processes."""
    t0 = time.time()
    df = _WORKER_DF
    assert df is not None, "worker not initialised"

    warmup = max(DEFAULT_WARMUP, params.ema_period + 50)

    # Monkey-patch EMA period on IndicatorEngine
    IndicatorEngine.ema200 = _make_ema200(params.ema_period)

    # Set CONFIG overrides
    CONFIG["adx_threshold"] = params.adx_threshold
    CONFIG["stop_loss"] = params.stop_loss_pct
    CONFIG["take_profit"] = params.take_profit_pct

    # Create engine
    engine = PaperTradingEngine(initial_balance=INITIAL_BALANCE)

    # Override non-CONFIG strategy params directly
    engine._strategy.rsi_period = params.rsi_period
    engine._strategy.rsi_oversold = params.rsi_oversold

    # Walk forward
    total_candles = len(df)
    trades, eq_curve = _run_walk_forward(
        df, engine, warmup, SYMBOL, TIMEFRAME, INITIAL_BALANCE,
    )

    walk_steps = total_candles - warmup
    result = compute_eval(params, trades, eq_curve, walk_steps, INITIAL_BALANCE)
    result.elapsed = round(time.time() - t0, 1)

    return result


# ---------------------------------------------------------------------------
#  Grid generation
# ---------------------------------------------------------------------------


def generate_grid() -> list[ParamSet]:
    """Produce every combination of optimisable parameters."""
    combos = list(product(
        EMA_VALUES,
        RSI_PERIOD_VALUES,
        RSI_OVERSOLD_VALUES,
        ADX_THRESHOLD_VALUES,
        TAKE_PROFIT_VALUES,
        STOP_LOSS_VALUES,
    ))
    return [
        ParamSet(ema, rsi_p, rsi_o, adx, tp, sl)
        for ema, rsi_p, rsi_o, adx, tp, sl in combos
    ]


# ---------------------------------------------------------------------------
#  Output helpers
# ---------------------------------------------------------------------------


METRIC_LABELS = [
    ("ema_period", "EMA"),
    ("rsi_period", "RSI"),
    ("rsi_oversold", "RSI_OS"),
    ("adx_threshold", "ADX"),
    ("take_profit_pct", "TP%"),
    ("stop_loss_pct", "SL%"),
    ("total_trades", "Trades"),
    ("net_profit", "NetProfit"),
    ("total_return_pct", "Return%"),
    ("profit_factor", "PF"),
    ("win_rate", "WinRate%"),
    ("max_drawdown_pct", "MaxDD%"),
    ("expectancy", "Expectancy"),
    ("sharpe_ratio", "Sharpe"),
    ("average_trade_pct", "AvgTrade%"),
    ("exposure_pct", "Exposure%"),
    ("elapsed", "Time_s"),
]

META_FIELDS = [
    "ema_period", "rsi_period", "rsi_oversold", "adx_threshold",
    "take_profit_pct", "stop_loss_pct",
]

METRIC_FIELDS = [
    "total_trades", "wins", "losses", "win_rate",
    "net_profit", "total_return_pct", "profit_factor",
    "gross_profit", "gross_loss",
    "average_win", "average_loss", "largest_win", "largest_loss",
    "expectancy", "sharpe_ratio",
    "average_trade_pct", "best_trade_pct", "worst_trade_pct",
    "average_holding_candles", "exposure_pct",
    "max_drawdown", "max_drawdown_pct",
    "final_equity", "equity_peak",
    "tp_count", "sl_count", "exit_count",
    "elapsed",
]


def _r(val: Any, decimals: int = 2) -> Any:
    """Round float if it's a float, else pass through."""
    if isinstance(val, float):
        return round(val, decimals)
    return val


def save_optimizer_csv(results: list[EvalResult], path: str) -> None:
    """Write every evaluation result to CSV."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = META_FIELDS + METRIC_FIELDS
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row: dict[str, Any] = {}
            d = asdict(r.params)
            for k in META_FIELDS:
                row[k] = _r(d.get(k, ""))
            for k in METRIC_FIELDS:
                row[k] = _r(getattr(r, k, ""))
            w.writerow(row)
    print(f"\n  CSV exported  : {path}")


def save_leaderboard(results: list[EvalResult], path: str) -> None:
    """Write a human-readable top-20 leaderboard."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    top20 = results[:20]

    hdr = (
        f"{'#':>3s}  {'EMA':>4s} {'RSI':>3s} {'OS':>3s} {'ADX':>3s} "
        f"{'TP%':>4s} {'SL%':>4s} {'Trades':>6s} "
        f"{'NetProfit':>10s} {'Return%':>8s} {'PF':>6s} "
        f"{'WinRate':>7s} {'MaxDD%':>7s} {'Sharpe':>7s} "
        f"{'Time':>6s}"
    )
    sep = "-" * len(hdr)

    lines = [hdr, sep]
    for rank, r in enumerate(top20, 1):
        pf = r.profit_factor
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        lines.append(
            f"{rank:3d}  {r.params.ema_period:4d} {r.params.rsi_period:3d} "
            f"{int(r.params.rsi_oversold):3d} {int(r.params.adx_threshold):3d} "
            f"{r.params.take_profit_pct:4.1f} {r.params.stop_loss_pct:4.1f} "
            f"{r.total_trades:6d} {r.net_profit:10.2f} "
            f"{r.total_return_pct:8.2f} {pf_str:>6s} "
            f"{r.win_rate:7.1f} {r.max_drawdown_pct:7.2f} "
            f"{r.sharpe_ratio:7.2f} {r.elapsed:6.1f}"
        )

    with open(path, "w") as f:
        f.write("ZETBOT AI — OPTIMIZER LEADERBOARD (sorted by net profit)\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Exchange: {EXCHANGE}  Symbol: {SYMBOL}  Timeframe: {TIMEFRAME}\n")
        f.write(f"Total combinations: {len(results)}\n\n")
        f.write("\n".join(lines))
        f.write("\n")

    print(f"  Leaderboard : {path}")


def save_best_json(best: EvalResult, path: str) -> None:
    """Write the best parameter set as JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = asdict(best.params)
    d["metrics"] = {k: _r(getattr(best, k)) for k in METRIC_FIELDS}
    with open(path, "w") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"  Best params : {path}")


def print_report(results: list[EvalResult], total: int, elapsed: float) -> None:
    """Print the top-20 leaderboard to console."""
    top20 = results[:20]

    print()
    print("=" * 100)
    print("  ZETBOT AI — STRATEGY OPTIMIZER RESULTS")
    print("=" * 100)
    print(f"  Combinations tested    : {total}")
    print(f"  Execution time         : {elapsed:.1f}s")
    print(f"  Exchange / Symbol / TF : {EXCHANGE} / {SYMBOL} / {TIMEFRAME}")
    print(f"  Date range             : {_WORKER_DF['timestamp'].iloc[0].date()} "
          f"→ {_WORKER_DF['timestamp'].iloc[-1].date()}"
          if _WORKER_DF is not None else "")
    print()

    hdr = (
        f"  {'#':>3s}  {'EMA':>4s} {'RSI':>3s} {'OS':>3s} {'ADX':>3s} "
        f"{'TP%':>4s} {'SL%':>4s} {'Trades':>6s} "
        f"{'NetProfit':>10s} {'Return%':>8s} {'PF':>6s} "
        f"{'WinRate':>7s} {'MaxDD%':>7s} {'Sharpe':>7s}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for rank, r in enumerate(top20, 1):
        pf = r.profit_factor
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(
            f"  {rank:3d}  {r.params.ema_period:4d} {r.params.rsi_period:3d} "
            f"{int(r.params.rsi_oversold):3d} {int(r.params.adx_threshold):3d} "
            f"{r.params.take_profit_pct:4.1f} {r.params.stop_loss_pct:4.1f} "
            f"{r.total_trades:6d} {r.net_profit:10.2f} "
            f"{r.total_return_pct:8.2f} {pf_str:>6s} "
            f"{r.win_rate:7.1f} {r.max_drawdown_pct:7.2f} "
            f"{r.sharpe_ratio:7.2f}"
        )

    print("=" * 100)
    print()

    # Print best parameter set
    best = results[0]
    print("  BEST PARAMETER SET")
    print(f"    EMA Period     : {best.params.ema_period}")
    print(f"    RSI Period     : {best.params.rsi_period}")
    print(f"    RSI Oversold   : {best.params.rsi_oversold}")
    print(f"    ADX Threshold  : {best.params.adx_threshold}")
    print(f"    Take Profit    : {best.params.take_profit_pct}%")
    print(f"    Stop Loss      : {best.params.stop_loss_pct}%")
    print(f"    Trades         : {best.total_trades}")
    print(f"    Net Profit     : ${best.net_profit:+,.2f}")
    print(f"    Return         : {best.total_return_pct:+.2f}%")
    print(f"    Profit Factor  : {pf_str}")
    print(f"    Win Rate       : {best.win_rate:.1f}%")
    print(f"    Max Drawdown   : ${best.max_drawdown:,.2f} ({best.max_drawdown_pct:.2f}%)")
    print(f"    Expectancy     : ${best.expectancy:+,.2f}")
    print(f"    Sharpe Ratio   : {best.sharpe_ratio:.4f}")
    print(f"    Exposure       : {best.exposure_pct:.1f}%")
    print()


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()

    print()
    print("=" * 100)
    print("  ZETBOT AI — STRATEGY OPTIMIZER")
    print("=" * 100)
    print(f"  Exchange  : {EXCHANGE}")
    print(f"  Symbol    : {SYMBOL}")
    print(f"  Timeframe : {TIMEFRAME}")
    print(f"  Candles   : {MAX_CANDLES}")
    print(f"  Balance   : ${INITIAL_BALANCE:,.2f}")
    print()

    # ── 1. Fetch data ────────────────────────────────────────────────
    print("  Fetching data … ", end="", flush=True)
    try:
        full_df = fetch_data()
    except Exception as exc:
        print(f"FAILED\n\n  Error: {exc}")
        sys.exit(1)

    print(f"{len(full_df)} candles fetched")
    print(f"  Range     : {full_df['timestamp'].iloc[0].date()} → "
          f"{full_df['timestamp'].iloc[-1].date()}")

    if len(full_df) < DEFAULT_WARMUP:
        print(f"\n  Error: need at least {DEFAULT_WARMUP} candles, got {len(full_df)}")
        sys.exit(1)

    # ── 2. Generate grid ──────────────────────────────────────────────
    param_grid = generate_grid()
    total_combos = len(param_grid)
    print(f"\n  Grid       : {total_combos} combinations")

    # Preview counts
    print(f"    EMA={EMA_VALUES} ({len(EMA_VALUES)})")
    print(f"    RSI_P={RSI_PERIOD_VALUES} ({len(RSI_PERIOD_VALUES)})")
    print(f"    RSI_OS={RSI_OVERSOLD_VALUES} ({len(RSI_OVERSOLD_VALUES)})")
    print(f"    ADX={ADX_THRESHOLD_VALUES} ({len(ADX_THRESHOLD_VALUES)})")
    print(f"    TP={TAKE_PROFIT_VALUES} ({len(TAKE_PROFIT_VALUES)})")
    print(f"    SL={STOP_LOSS_VALUES} ({len(STOP_LOSS_VALUES)})")

    # ── 3. Distribute across workers ──────────────────────────────────
    cpu_count = os.cpu_count() or 4
    workers = max(1, cpu_count - 1)  # leave one core free
    print(f"\n  Workers    : {workers}")
    print()

    print("  Running optimizer …")
    results: list[EvalResult] = []
    batch_size = max(1, total_combos // 100)

    with Pool(processes=workers, initializer=_worker_init, initargs=(full_df,)) as pool:
        for idx, res in enumerate(pool.imap_unordered(evaluate_one, param_grid), 1):
            results.append(res)
            if idx % batch_size == 0 or idx == total_combos:
                pct = idx * 100 // total_combos
                print(f"\r    {pct:3d}%  ({idx:4d}/{total_combos})", end="", flush=True)

    print()
    print()

    # ── 4. Sort by net profit (descending) ────────────────────────────
    results.sort(key=lambda r: r.net_profit, reverse=True)

    elapsed = time.time() - t0

    # ── 5. Output ─────────────────────────────────────────────────────
    print_report(results, total_combos, elapsed)

    csv_path = "data/optimizer_results.csv"
    save_optimizer_csv(results, csv_path)

    leaderboard_path = "data/leaderboard.txt"
    save_leaderboard(results, leaderboard_path)

    best_path = "data/best_parameters.json"
    save_best_json(results[0], best_path)

    # ── 6. Footer ─────────────────────────────────────────────────────
    print(f"\n  Completed at : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Duration     : {elapsed:.1f}s")
    print("=" * 100)
    print()


if __name__ == "__main__":
    main()
