"""
Professional Position Manager for ZetBot AI

Manages active positions after execution.  Simulates the full lifecycle
including breakeven, trailing stops, partial take-profit, trend exits,
and timeouts.  Does NOT send exchange orders.

Usage::

    python -m scripts.position_manager
"""

import csv
import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

TRADE_PLAN_PATH = "data/trade_plan.json"
SCANNER_PATH = "data/scanner_results.json"

# Trailing stop
TRAIL_ATR_MULTIPLIER = 2.0

# Partial take-profit allocation (% of position sold at each level)
TP1_SELL_PCT = 30.0
TP2_SELL_PCT = 30.0
TP3_SELL_PCT = 40.0  # remaining

# Maximum holding time
MAX_HOLDING_CANDLES = 48    # 48 hours at 1h timeframe
TIMEFRAME_HOURS = 1

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class TradePlan:
    symbol: str
    entry_price: float
    position_size_usdt: float
    quantity: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_amount: float
    reward_amount: float
    risk_reward: float
    probability: float
    recommendation: str
    confidence: float
    signal_time: str
    status: str
    rejection_reason: str


@dataclass
class ScannerPrice:
    symbol: str
    price: float
    atr_pct: float
    trend_alignment: str


@dataclass
class Position:
    """Complete state of one managed position."""
    symbol: str
    entry_price: float
    current_price: float
    position_size_usdt: float
    quantity: float
    remaining_pct: float
    remaining_qty: float
    floating_pnl: float
    floating_pnl_pct: float
    realized_pnl: float
    total_pnl: float
    highest_price: float
    lowest_price: float
    stop_loss: float
    current_stop: float
    tp1: float
    tp2: float
    tp3: float
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    breakeven_active: bool
    trailing_active: bool
    holding_candles: int
    holding_hours: float
    entry_time: str
    status: str


# ---------------------------------------------------------------------------
#  Data loader
# ---------------------------------------------------------------------------


class DataLoader:
    """Load trade plans and current market prices."""

    @staticmethod
    def load_plans(path: str) -> list[TradePlan]:
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        plans: list[TradePlan] = []
        for p in data.get("plans", []):
            if p.get("status") != "READY":
                continue
            plans.append(TradePlan(
                symbol=p["symbol"],
                entry_price=p.get("entry_price", 0.0),
                position_size_usdt=p.get("position_size_usdt", 0.0),
                quantity=p.get("quantity", 0.0),
                stop_loss=p.get("stop_loss", 0.0),
                tp1=p.get("tp1", 0.0),
                tp2=p.get("tp2", 0.0),
                tp3=p.get("tp3", 0.0),
                risk_amount=p.get("risk_amount", 0.0),
                reward_amount=p.get("reward_amount", 0.0),
                risk_reward=p.get("risk_reward", 0.0),
                probability=p.get("probability", 0.0),
                recommendation=p.get("recommendation", ""),
                confidence=p.get("confidence", 0.0),
                signal_time=p.get("signal_time", ""),
                status=p.get("status", ""),
                rejection_reason=p.get("rejection_reason", ""),
            ))
        return plans

    @staticmethod
    def load_prices(path: str) -> dict[str, ScannerPrice]:
        with open(path) as f:
            data = json.load(f)
        result: dict[str, ScannerPrice] = {}
        for p in data.get("pairs", []):
            result[p["symbol"]] = ScannerPrice(
                symbol=p["symbol"],
                price=p.get("price", 0.0),
                atr_pct=p.get("atr_pct", 0.0),
                trend_alignment=p.get("trend_alignment", "MIXED"),
            )
        return result


# ---------------------------------------------------------------------------
#  Breakeven manager
# ---------------------------------------------------------------------------


class BreakevenManager:
    """Move stop-loss to entry price once TP1 has been reached."""

    @staticmethod
    def apply(entry_price: float, tp1_hit: bool,
              current_stop: float) -> tuple[float, bool]:
        if tp1_hit and current_stop < entry_price:
            return entry_price, True
        return current_stop, False


# ---------------------------------------------------------------------------
#  Trailing stop manager
# ---------------------------------------------------------------------------


class TrailingStopManager:
    """ATR-based trailing stop activated after TP2."""

    @staticmethod
    def apply(*, current_price: float, current_stop: float,
              atr_pct: float, tp2_hit: bool) -> float:
        if not tp2_hit:
            return current_stop
        atr_value = current_price * (atr_pct / 100.0)
        trail_stop = current_price - atr_value * TRAIL_ATR_MULTIPLIER
        return max(current_stop, trail_stop)


# ---------------------------------------------------------------------------
#  Partial take-profit
# ---------------------------------------------------------------------------


class PartialTakeProfit:
    """Allocate position percentages across TP levels."""

    @staticmethod
    def remaining(initial_qty: float,
                  tp1_hit: bool, tp2_hit: bool, tp3_hit: bool) -> float:
        if tp3_hit:
            return 0.0
        if tp2_hit:
            return TP3_SELL_PCT / 100.0
        if tp1_hit:
            return (TP2_SELL_PCT + TP3_SELL_PCT) / 100.0
        return 1.0

    @staticmethod
    def realized_pnl(plan: TradePlan,
                     tp1_hit: bool, tp2_hit: bool,
                     tp3_hit: bool) -> float:
        pnl = 0.0
        if tp1_hit:
            pnl += (plan.tp1 - plan.entry_price) * plan.quantity \
                * (TP1_SELL_PCT / 100.0)
        if tp2_hit:
            pnl += (plan.tp2 - plan.entry_price) * plan.quantity \
                * (TP2_SELL_PCT / 100.0)
        if tp3_hit:
            pnl += (plan.tp3 - plan.entry_price) * plan.quantity \
                * (TP3_SELL_PCT / 100.0)
        return pnl


# ---------------------------------------------------------------------------
#  Position simulator
# ---------------------------------------------------------------------------


class PositionSimulator:
    """Determine the current state of a position given market data."""

    @staticmethod
    def simulate(
        plan: TradePlan,
        current_price: float,
        atr_pct: float,
        trend_alignment: str,
        now: datetime,
    ) -> Position:
        entry = plan.entry_price
        qty = plan.quantity
        initial_stop = plan.stop_loss
        tp1, tp2, tp3 = plan.tp1, plan.tp2, plan.tp3

        # --- Determine which levels have been reached ---
        tp1_hit = current_price >= tp1
        tp2_hit = current_price >= tp2
        tp3_hit = current_price >= tp3

        # --- Stop check ---
        stopped = current_price <= initial_stop

        # --- Breakeven ---
        current_stop = initial_stop
        be_active = False
        if tp1_hit and not stopped:
            current_stop, be_active = BreakevenManager.apply(
                entry, tp1_hit, current_stop,
            )
            if current_price <= current_stop and be_active:
                stopped = True

        # --- Trailing ---
        trailing_active = False
        if tp2_hit and not stopped:
            current_stop = TrailingStopManager.apply(
                current_price=current_price,
                current_stop=current_stop,
                atr_pct=atr_pct,
                tp2_hit=tp2_hit,
            )
            if current_stop > initial_stop:
                trailing_active = True

        # --- Partial TP allocation ---
        remaining_pct = PartialTakeProfit.remaining(
            qty, tp1_hit, tp2_hit, tp3_hit,
        )
        realized = PartialTakeProfit.realized_pnl(
            plan, tp1_hit, tp2_hit, tp3_hit,
        )

        remaining_qty = qty * remaining_pct

        # --- Trend exit ---
        trend_exit = False
        if not stopped and not tp3_hit \
                and trend_alignment not in ("BULLISH",):
            trend_exit = True

        # --- Holding time ---
        holding_hours = 0.0
        holding_candles = 0
        try:
            signal_dt = datetime.fromisoformat(plan.signal_time)
            delta = now - signal_dt
            holding_hours = delta.total_seconds() / 3600.0
            holding_candles = max(0, int(holding_hours / TIMEFRAME_HOURS))
        except (ValueError, TypeError):
            pass

        timeout = holding_candles >= MAX_HOLDING_CANDLES

        # --- Status ---
        if stopped:
            status = "STOPPED"
        elif tp3_hit:
            status = "CLOSED"
        elif trend_exit:
            status = "CLOSED"
        elif timeout:
            status = "TIMEOUT"
        elif trailing_active:
            status = "TRAILING"
        elif be_active:
            status = "BREAKEVEN"
        elif tp1_hit:
            status = "PARTIAL"
        else:
            status = "OPEN"

        # --- Prices ---
        highest = max(entry, current_price)
        lowest = min(entry, current_price)

        # --- PnL ---
        unrealized = (current_price - entry) * remaining_qty
        total_pnl = realized + unrealized

        pos_value = plan.position_size_usdt
        fl_pnl_pct = (total_pnl / pos_value * 100.0) if pos_value > 0 else 0.0

        return Position(
            symbol=plan.symbol,
            entry_price=entry,
            current_price=current_price,
            position_size_usdt=round(pos_value, 2),
            quantity=round(qty, 4),
            remaining_pct=round(remaining_pct * 100.0, 1),
            remaining_qty=round(remaining_qty, 4),
            floating_pnl=round(total_pnl, 2),
            floating_pnl_pct=round(fl_pnl_pct, 2),
            realized_pnl=round(realized, 2),
            total_pnl=round(total_pnl, 2),
            highest_price=round(highest, 8),
            lowest_price=round(lowest, 8),
            stop_loss=round(initial_stop, 8),
            current_stop=round(current_stop, 8),
            tp1=round(tp1, 8),
            tp2=round(tp2, 8),
            tp3=round(tp3, 8),
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            tp3_hit=tp3_hit,
            breakeven_active=be_active,
            trailing_active=trailing_active,
            holding_candles=holding_candles,
            holding_hours=round(holding_hours, 1),
            entry_time=plan.signal_time,
            status=status,
        )


# ---------------------------------------------------------------------------
#  PositionManager (orchestrator)
# ---------------------------------------------------------------------------


class PositionManager:
    """Orchestrate the full position management pipeline."""

    def __init__(self) -> None:
        self.positions: list[Position] = []

    @staticmethod
    def _load_previous_positions(path: str) -> dict[str, dict]:
        """Load previous positions.json as a dict keyed by symbol."""
        try:
            with open(path) as f:
                data = json.load(f)
            return {p["symbol"]: p for p in data.get("positions", [])}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def run(self) -> list[Position]:
        """Full position management pipeline."""
        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — PROFESSIONAL POSITION MANAGER")
        print(f"  {'=' * 78}")

        t0 = time.time()
        now = datetime.now(timezone.utc)

        # 1. Load
        print("  [1/4] Loading data … ", end="", flush=True)
        plans = DataLoader.load_plans(TRADE_PLAN_PATH)
        prices = DataLoader.load_prices(SCANNER_PATH)
        prev_positions = self._load_previous_positions("data/positions.json")
        print(f"{len(plans)} plans, {len(prices)} scanner prices, "
              f"{len(prev_positions)} prev positions")

        if not plans:
            print("  No READY plans found.  Exiting.")
            return []

        # 2. Simulate
        print("  [2/4] Simulating positions …", flush=True)
        positions: list[Position] = []
        for plan in plans:
            sp = prices.get(plan.symbol)
            if sp is None:
                print(f"        Warning: no price data for {plan.symbol}, "
                      f"using entry price")
                cur_price = plan.entry_price
                atr_pct = 0.0
                trend = "MIXED"
            else:
                cur_price = sp.price
                atr_pct = sp.atr_pct
                trend = sp.trend_alignment

            prev = prev_positions.get(plan.symbol)
            if prev:
                prev_entry_time = prev.get("entry_time", "")
                if prev_entry_time and prev.get("status") in OPEN_STATUSES:
                    plan.signal_time = prev_entry_time

            pos = PositionSimulator.simulate(
                plan, cur_price, atr_pct, trend, now,
            )
            positions.append(pos)

        self.positions = positions

        # 3. Report
        elapsed = time.time() - t0
        print("  [3/4] Generating report …", flush=True)
        self._print_summary(elapsed)

        return positions

    def _print_summary(self, elapsed: float) -> None:
        positions = self.positions

        open_ = [p for p in positions if p.status == "OPEN"]
        partial = [p for p in positions if p.status == "PARTIAL"]
        trailing = [p for p in positions if p.status == "TRAILING"]
        be = [p for p in positions if p.status == "BREAKEVEN"]
        closed = [p for p in positions if p.status == "CLOSED"]
        stopped = [p for p in positions if p.status == "STOPPED"]
        timeout = [p for p in positions if p.status == "TIMEOUT"]
        active = [p for p in positions if p.status in OPEN_STATUSES]

        print()
        print(f"  {'=' * 78}")
        print(f"  POSITION MANAGER — RESULTS")
        print(f"  {'=' * 78}")
        print(f"  Total positions : {len(positions)}")
        print(f"    OPEN          : {len(open_)}")
        print(f"    PARTIAL       : {len(partial)}")
        print(f"    TRAILING      : {len(trailing)}")
        print(f"    BREAKEVEN     : {len(be)}")
        print(f"    CLOSED        : {len(closed)}")
        print(f"    STOPPED       : {len(stopped)}")
        print(f"    TIMEOUT       : {len(timeout)}")
        print()

        if active:
            total_fl = sum(p.floating_pnl for p in active)
            avg_hold = (
                sum(p.holding_hours for p in active) / len(active)
            )
            total_value = sum(p.position_size_usdt for p in active)
            print(f"  Active Position Summary (n={len(active)}):")
            print(f"    Total Value       : ${total_value:>8,.2f}")
            print(f"    Total Floating PnL: ${total_fl:>+8,.2f}")
            print(f"    Average Holding   : {avg_hold:.1f}h")
            print()

        print(f"  Execution time : {elapsed:.2f}s")
        print(f"  {'=' * 78}")
        print()

        # Leaderboard
        if active:
            active.sort(key=lambda p: p.floating_pnl, reverse=True)
            print(f"  ACTIVE POSITIONS:")
            hdr = (
                f"  {'#':>3s} {'Pair':>12s} {'Status':>10s} "
                f"{'PnL $':>10s} {'PnL%':>7s} {'Entry':>10s} "
                f"{'Price':>10s} {'Stop':>10s} {'Hld':>4s}"
            )
            print(hdr)
            print(f"  {'-' * (len(hdr) - 2)}")

            for i, p in enumerate(active, 1):
                pnl_str = f"${p.floating_pnl:+,.2f}"
                print(
                    f"  {i:3d} {p.symbol:>12s} {p.status:>10s} "
                    f"{pnl_str:>10s} {p.floating_pnl_pct:>+7.2f} "
                    f"{p.entry_price:>10.4f} {p.current_price:>10.4f} "
                    f"{p.current_stop:>10.4f} {p.holding_hours:>4.0f}"
                )
            print()

        # Closed positions
        done = [p for p in positions if p.status in CLOSED_STATUSES]
        if done:
            done.sort(key=lambda p: p.total_pnl, reverse=True)
            print(f"  CLOSED POSITIONS:")
            hdr = (
                f"  {'#':>3s} {'Pair':>12s} {'Status':>10s} "
                f"{'PnL $':>10s} {'Entry':>10s} {'Exit':>10s} "
                f"{'Hld':>4s}"
            )
            print(hdr)
            print(f"  {'-' * (len(hdr) - 2)}")

            for i, p in enumerate(done, 1):
                pnl_str = f"${p.total_pnl:+,.2f}"
                exit_method = p.status
                print(
                    f"  {i:3d} {p.symbol:>12s} {exit_method:>10s} "
                    f"{pnl_str:>10s} {p.entry_price:>10.4f} "
                    f"{p.current_price:>10.4f} {p.holding_hours:>4.0f}"
                )
            print()


# ---------------------------------------------------------------------------
#  File export
# ---------------------------------------------------------------------------


class PositionExport:
    """Write positions to CSV and JSON."""

    @staticmethod
    def to_csv(positions: list[Position], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "symbol", "entry_price", "current_price",
            "position_size_usdt", "quantity",
            "remaining_pct", "remaining_qty",
            "floating_pnl", "floating_pnl_pct",
            "realized_pnl", "total_pnl",
            "highest_price", "lowest_price",
            "stop_loss", "current_stop",
            "tp1", "tp2", "tp3",
            "tp1_hit", "tp2_hit", "tp3_hit",
            "breakeven_active", "trailing_active",
            "holding_candles", "holding_hours",
            "entry_time", "status",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for pos in positions:
                row = asdict(pos)
                w.writerow({k: row[k] for k in fields})
        print(f"  CSV exported   : {path}")

    @staticmethod
    def to_json(positions: list[Position], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_positions": len(positions),
            "active_count": sum(
                1 for p in positions
                if p.status in OPEN_STATUSES
            ),
            "closed_count": sum(
                1 for p in positions
                if p.status in CLOSED_STATUSES
            ),
            "positions": [asdict(p) for p in positions],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  JSON export    : {path}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    manager = PositionManager()
    positions = manager.run()

    if not positions:
        return

    csv_path = "data/positions.csv"
    PositionExport.to_csv(positions, csv_path)

    json_path = "data/positions.json"
    PositionExport.to_json(positions, json_path)

    print(f"\n  Completed at  : {datetime.now(timezone.utc).isoformat()}")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    main()
