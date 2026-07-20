"""Single source of truth for all bot statistics and account state.

Reads from canonical JSON files written by the pipeline and engine so
that every command sees the same values.  Provides a unified
``AccountSnapshot`` dataclass and computed metrics (streaks, expectancy,
daily/weekly/monthly aggregates).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from scripts.position_status import is_open


@dataclass
class AccountSnapshot:
    """Immutable point-in-time view of the account / portfolio.

    All values are computed from canonical JSON files and open positions.
    Every Telegram command MUST read from this — never compute independently.

    Invariants:
        equity == balance + position_value
        net_pnl == realized_pnl + unrealized_pnl
        exposure_pct == (position_value / equity * 100) if equity > 0 else 0
    """
    balance: float = 0.0
    equity: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    net_pnl: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    open_positions: int = 0
    initial_balance: float = 0.0
    position_value: float = 0.0
    exposure_pct: float = 0.0


@dataclass
class ComputedMetrics:
    """Extended metrics derived from trade history."""
    avg_holding_hours: float = 0.0
    avg_roi: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    expectancy: float = 0.0
    win_streak: int = 0
    loss_streak: int = 0
    best_trade: dict[str, Any] = field(default_factory=dict)
    worst_trade: dict[str, Any] = field(default_factory=dict)


class MetricsManager:
    """Central source of truth for all account & performance data.

    All data is read from the canonical JSON files.  No in-memory
    mutable state is kept, so every call reflects the latest persisted
    snapshot.
    """

    def __init__(self, data_dir: str = "data") -> None:
        self._data_dir = data_dir

    # ------------------------------------------------------------------
    #  File readers
    # ------------------------------------------------------------------

    def _read_json(self, filename: str) -> dict[str, Any]:
        path = os.path.join(self._data_dir, filename)
        try:
            with open(path) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _read_orders(self) -> list[dict[str, Any]]:
        d = self._read_json("paper_orders.json")
        return d if isinstance(d, list) else d.get("orders", [])

    def _read_positions(self) -> list[dict[str, Any]]:
        d = self._read_json("positions.json")
        return d if isinstance(d, list) else d.get("positions", [])

    def _read_balance_pb(self) -> dict[str, Any]:
        return self._read_json("paper_balance.json")

    # ------------------------------------------------------------------
    #  Account snapshot
    # ------------------------------------------------------------------

    def account(self) -> AccountSnapshot:
        """Compute the authoritative account snapshot.

        ``paper_balance.json`` is written by ``scripts.paper_trading_engine``
        (``PaperExport.balance_json``) from the exact same in-memory
        ``VirtualPosition`` objects, at the exact same instant, as the
        ``positions.json`` it also writes (see ``_save_state`` /
        ``PaperTradingEngine.run``). It is therefore the authoritative
        accounting output — balance, equity, realized/unrealized/net PnL,
        and total_return_pct are read directly from it, never
        recomputed here. Recomputing them independently from
        positions.json would be a second, divergence-prone accounting
        implementation of the same numbers.

        positions.json is used ONLY to identify which records are
        currently open (open_positions / open_positions_count and
        position listings) — never to derive a dollar figure.

        Invariants (always true, by construction):
            equity == balance + position_value
            net_pnl == realized_pnl + unrealized_pnl
            exposure_pct == (position_value / equity * 100) if equity > 0 else 0
        """
        pb = self._read_balance_pb()
        bal = pb.get("final_balance", 0.0)
        realized = pb.get("realized_pnl", 0.0)
        initial = pb.get("initial_balance", 10_000.0)
        equity = pb.get("final_equity", bal)
        unrealized = pb.get("unrealized_pnl", 0.0)
        net = pb.get("net_pnl", realized + unrealized)
        total_return_pct = pb.get(
            "total_return_pct",
            ((equity - initial) / initial * 100.0) if initial > 0 else 0.0,
        )

        open_count = self.open_positions_count()

        # Runtime invariant enforcement: when no positions are open,
        # unrealized_pnl MUST be zero and equity MUST equal balance.
        # paper_balance.json can contain stale unrealized_pnl from a
        # recently-closed position that hasn't been reconciled yet.
        if open_count == 0:
            unrealized = 0.0
            net = realized
            equity = bal
            total_return_pct = (
                ((equity - initial) / initial * 100.0) if initial > 0 else 0.0
            )

        # position_value is derived from the authoritative equity/balance
        # (equity == balance + position_value by construction in the
        # paper engine's own VirtualWallet.snapshot()) rather than summed
        # from positions.json, so it can never disagree with equity/exposure.
        position_value = equity - bal
        exposure_pct = (position_value / equity * 100.0) if equity > 0 else 0.0

        return AccountSnapshot(
            balance=bal,
            equity=equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            net_pnl=net,
            total_return_pct=total_return_pct,
            total_trades=pb.get("total_trades", 0),
            winning_trades=pb.get("winning_trades", 0),
            losing_trades=pb.get("losing_trades", 0),
            win_rate=pb.get("win_rate", 0.0),
            profit_factor=pb.get("profit_factor", 0.0),
            gross_profit=pb.get("gross_profit", 0.0),
            gross_loss=pb.get("gross_loss", 0.0),
            open_positions=open_count,
            initial_balance=initial,
            position_value=position_value,
            exposure_pct=exposure_pct,
        )

    # ------------------------------------------------------------------
    #  Positions
    # ------------------------------------------------------------------

    def open_positions_count(self) -> int:
        return sum(1 for p in self._read_positions() if is_open(p.get("status")))

    def open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self._read_positions() if is_open(p.get("status"))]

    def all_positions(self) -> list[dict[str, Any]]:
        return self._read_positions()

    # ------------------------------------------------------------------
    #  Orders / Trade history
    # ------------------------------------------------------------------

    def closed_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._read_orders() if o.get("status") == "CLOSED"]

    def buy_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._read_orders() if o.get("side") == "BUY"]

    def trade_history(self) -> list[dict[str, Any]]:
        """Return closed BUY orders enriched with a matched SELL if any.

        Each result is one completed trade with::
            symbol, side, entry_price, exit_price, quantity,
            entry_time, exit_time, holding_hours,
            net_pnl, net_pnl_pct, reason, result.
        """
        all_orders = self._read_orders()
        buys = [o for o in all_orders if o.get("side") == "BUY"]
        sells = [o for o in all_orders if o.get("side") == "SELL"]

        trades: list[dict[str, Any]] = []
        matched_sells: set[int] = set()

        for b in buys:
            bid = id(b)
            if bid in matched_sells:
                continue

            entry_px = b.get("fill_price", 0.0) or b.get("entry_price", 0.0)
            entry_time = b.get("filled_at") or b.get("created_at", "")
            symbol = b.get("symbol", "")
            qty = b.get("quantity", 0.0) or b.get("filled_quantity", 0.0)

            # Find matching sell for this buy
            best_sell: Optional[dict[str, Any]] = None
            for s in sells:
                if s.get("symbol") == symbol and id(s) not in matched_sells:
                    if best_sell is None or (s.get("closed_at") or "") > (best_sell.get("closed_at") or ""):
                        best_sell = s

            if best_sell is not None:
                matched_sells.add(id(best_sell))
                exit_px = best_sell.get("fill_price", 0.0) or best_sell.get("exit_price", 0.0)
                exit_time = best_sell.get("closed_at") or best_sell.get("filled_at", "")
                net_pnl = best_sell.get("net_pnl", 0.0)
                net_pnl_pct = best_sell.get("net_pnl_pct", 0.0)
                reason = best_sell.get("close_reason", "") or best_sell.get("reason", "")
                result = "WIN" if net_pnl >= 0 else "LOSS"
            else:
                # BUY not yet closed
                continue

            holding_hours = 0.0
            if entry_time and exit_time:
                try:
                    fmt = "%Y-%m-%dT%H:%M:%S.%f"
                    et = datetime.strptime(entry_time.split("+")[0].split("Z")[0], fmt)
                    xt = datetime.strptime(exit_time.split("+")[0].split("Z")[0], fmt)
                    holding_hours = round((xt - et).total_seconds() / 3600, 2)
                except (ValueError, IndexError):
                    pass

            trades.append({
                "symbol": symbol,
                "side": "SELL",
                "entry_price": entry_px,
                "exit_price": exit_px,
                "quantity": qty,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "holding_hours": holding_hours,
                "net_pnl": net_pnl,
                "net_pnl_pct": net_pnl_pct,
                "reason": reason or "TP/SL",
                "result": result,
            })

        trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
        return trades

    # ------------------------------------------------------------------
    #  Computed metrics
    # ------------------------------------------------------------------

    def computed(self, trades: Optional[list[dict[str, Any]]] = None) -> ComputedMetrics:
        if trades is None:
            trades = self.trade_history()

        if not trades:
            return ComputedMetrics()

        total = len(trades)
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) <= 0]

        winning_pnls = [t["net_pnl"] for t in wins]
        losing_pnls = [abs(t["net_pnl"]) for t in losses]

        avg_holding = sum(t.get("holding_hours", 0) for t in trades) / total
        avg_roi = sum(t.get("net_pnl_pct", 0) for t in trades) / total
        avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
        avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
        largest_win = max(winning_pnls) if winning_pnls else 0.0
        largest_loss = max(losing_pnls) if losing_pnls else 0.0
        expectancy = (sum(winning_pnls) - sum(losing_pnls)) / total

        # Streaks
        win_streak = 0
        loss_streak = 0
        current_win = 0
        current_loss = 0
        for t in reversed(trades):
            if t.get("net_pnl", 0) > 0:
                current_win += 1
                current_loss = 0
            else:
                current_loss += 1
                current_win = 0
            win_streak = max(win_streak, current_win)
            loss_streak = max(loss_streak, current_loss)

        best = max(trades, key=lambda t: t.get("net_pnl", 0))
        worst = min(trades, key=lambda t: t.get("net_pnl", 0))

        return ComputedMetrics(
            avg_holding_hours=avg_holding,
            avg_roi=avg_roi,
            avg_win=avg_win,
            avg_loss=avg_loss,
            largest_win=largest_win,
            largest_loss=largest_loss,
            expectancy=expectancy,
            win_streak=win_streak,
            loss_streak=loss_streak,
            best_trade=best,
            worst_trade=worst,
        )

    # ------------------------------------------------------------------
    #  Daily / Weekly / Monthly filters
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(ts: str) -> Optional[datetime]:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                clean = ts.split("+")[0].split("Z")[0]
                return datetime.strptime(clean, fmt)
            except (ValueError, IndexError):
                continue
        return None

    def trades_since(self, cutoff: datetime) -> list[dict[str, Any]]:
        return [
            t for t in self.trade_history()
            if t.get("exit_time") and (
                dt := self._parse_ts(t["exit_time"])
            ) and dt >= cutoff
        ]

    def today_trades(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.trades_since(today_start)

    def today_summary(self) -> dict[str, Any]:
        trades = self.today_trades()
        total = len(trades)
        wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("net_pnl", 0) <= 0)
        pnl = sum(t.get("net_pnl", 0) for t in trades)
        roi = sum(t.get("net_pnl_pct", 0) for t in trades)
        avg_hold = sum(t.get("holding_hours", 0) for t in trades) / total if total else 0.0
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total else 0.0,
            "pnl": pnl,
            "roi": roi,
            "avg_holding_hours": avg_hold,
        }

    # ------------------------------------------------------------------
    #  Scanner / market helpers
    # ------------------------------------------------------------------

    def scanner_results(self) -> dict[str, Any]:
        return self._read_json("scanner_results.json")

    # ------------------------------------------------------------------
    #  Volume formatting
    # ------------------------------------------------------------------

    @staticmethod
    def fmt_volume(val: float) -> str:
        if val >= 1_000_000:
            return f"{val / 1_000_000:.2f}M"
        if val >= 1_000:
            return f"{val / 1_000:.2f}K"
        return f"{val:.2f}"
