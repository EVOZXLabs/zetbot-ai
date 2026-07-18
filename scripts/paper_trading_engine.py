"""
Professional Paper Trading Engine for ZetBot AI

Simulates execution of READY trade plans from the trade executor
without placing real exchange orders.  Tracks virtual balance,
positions, orders, fees, slippage, and generates full trade history
and performance metrics.

Usage::

    python -m scripts.paper_trading_engine
"""

import csv
import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

TRADE_PLAN_PATH = "data/trade_plan.json"
POSITIONS_PATH = "data/positions.json"
STATE_PATH = "data/paper_state.json"

INITIAL_BALANCE = 10_000.0
TAKER_FEE = 0.001           # 0.1 %
MAKER_FEE = 0.00075         # 0.075 %
SLIPPAGE_BPS = 3            # 3 bps market-order slippage

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class Order:
    """A single virtual order."""
    id: str
    symbol: str
    side: str                # BUY | SELL
    type: str                # MARKET | LIMIT
    quantity: float
    filled_quantity: float
    entry_price: float
    fill_price: float
    slippage: float
    entry_fee: float
    exit_price: float
    exit_fee: float
    total_cost: float
    total_proceeds: float
    net_pnl: float
    net_pnl_pct: float
    status: str              # NEW | FILLED | PARTIALLY_FILLED | CLOSED | STOPPED | CANCELLED
    created_at: str
    filled_at: str
    closed_at: str
    exit_reason: str = ""


@dataclass
class VirtualPosition:
    """An open virtual position."""
    symbol: str
    order_id: str
    quantity: float
    remaining_qty: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    cost_basis: float
    status: str              # OPEN | CLOSED
    tp1_sold: bool = False   # TP level already executed
    tp2_sold: bool = False
    tp3_sold: bool = False
    opened_at: str = ""      # ISO timestamp of first open
    signal_time: str = ""
    closure_notified: bool = False


@dataclass
class EquitySnapshot:
    timestamp: str
    balance: float
    equity: float
    unrealized_pnl: float
    margin_used: float
    free_balance: float


# ---------------------------------------------------------------------------
#  Virtual wallet
# ---------------------------------------------------------------------------


class VirtualWallet:
    """Tracks virtual USDT balance and margin.

    Accounting rules (spot):
        equity   = free_balance + position_market_value
        free     = balance (all free USDT, since no margin in spot)
        used     = cost basis of open positions (not tracked here)
        realized_pnl  = cumulative closed P&L
        unrealized_pnl = sum of open position floating P&L
    """

    def __init__(self, initial: float = INITIAL_BALANCE) -> None:
        self.initial = initial
        self.balance = initial
        self.margin_used = 0.0

    @property
    def free_balance(self) -> float:
        return max(0.0, self.balance - self.margin_used)

    @property
    def equity(self) -> float:
        """USDT equity = free balance (spot accounts have no margin)."""
        return self.balance

    def reserve(self, amount: float) -> bool:
        if amount > self.free_balance:
            return False
        self.margin_used += amount
        return True

    def release(self, amount: float) -> None:
        self.margin_used = max(0.0, self.margin_used - amount)

    def deduct(self, amount: float) -> bool:
        if amount > self.balance:
            return False
        self.balance -= amount
        self.margin_used = max(
            0.0, self.margin_used - min(self.margin_used, amount)
        )
        return True

    def add(self, amount: float) -> None:
        self.balance += amount

    def snapshot(self, position_value: float = 0.0,
                 unrealized_pnl_value: float = 0.0) -> EquitySnapshot:
        """"""
        equity = self.balance + position_value
        return EquitySnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            balance=round(self.balance, 2),
            equity=round(equity, 2),
            unrealized_pnl=round(unrealized_pnl_value, 2),
            margin_used=round(self.margin_used, 2),
            free_balance=round(self.free_balance, 2),
        )


# ---------------------------------------------------------------------------
#  Fee & slippage calculator
# ---------------------------------------------------------------------------


class ExecutionModel:
    """Apply fees and slippage to simulated orders."""

    @staticmethod
    def buy(entry_price: float, quantity: float) -> dict[str, float]:
        slippage = entry_price * (SLIPPAGE_BPS / 10000.0)
        fill_price = entry_price + slippage
        gross_cost = fill_price * quantity
        fee = gross_cost * TAKER_FEE
        total_cost = gross_cost + fee
        return {
            "fill_price": fill_price,
            "slippage": slippage,
            "fee": fee,
            "total_cost": total_cost,
        }

    @staticmethod
    def sell(exit_price: float, quantity: float) -> dict[str, float]:
        slippage = exit_price * (SLIPPAGE_BPS / 10000.0)
        fill_price = exit_price - slippage
        gross_proceeds = fill_price * quantity
        fee = gross_proceeds * TAKER_FEE
        total_proceeds = gross_proceeds - fee
        return {
            "fill_price": fill_price,
            "slippage": slippage,
            "fee": fee,
            "total_proceeds": total_proceeds,
        }


# ---------------------------------------------------------------------------
#  Order ID
# ---------------------------------------------------------------------------

_order_counter: int = 0


def _next_order_id() -> str:
    global _order_counter
    _order_counter += 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"PAPER-{ts}-{_order_counter:04d}"


# ---------------------------------------------------------------------------
#  Metrics
# ---------------------------------------------------------------------------


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den != 0 else default


class MetricsCalculator:
    """Compute performance metrics from closed orders and equity history."""

    @staticmethod
    def compute(
        orders: list[Order],
        equity_snapshots: list[EquitySnapshot],
        initial_balance: float,
        unrealized_pnl: float = 0.0,
    ) -> dict[str, Any]:
        closed = [o for o in orders if o.status == "CLOSED"]
        winners = [o for o in closed if o.net_pnl > 0]
        losers = [o for o in closed if o.net_pnl <= 0]

        total_trades = len(closed)
        wins = len(winners)
        losses = len(losers)
        win_rate = _safe_div(wins, total_trades) * 100.0 if total_trades else 0.0

        gross_profit = sum(o.net_pnl for o in winners)
        gross_loss = abs(sum(o.net_pnl for o in losers))
        net_pnl = gross_profit - gross_loss
        profit_factor = _safe_div(gross_profit, gross_loss, float("inf"))
        profit_factor = round(profit_factor, 2)

        # Drawdown from equity history
        max_dd = 0.0
        max_dd_pct = 0.0
        peak = initial_balance
        for snap in equity_snapshots:
            eq = snap.equity
            if eq > peak:
                peak = eq
            dd = peak - eq
            dd_pct = _safe_div(dd, peak) * 100.0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct

        final_eq = equity_snapshots[-1].equity if equity_snapshots else initial_balance
        total_return = _safe_div(final_eq - initial_balance, initial_balance) * 100.0

        return {
            "initial_balance": round(initial_balance, 2),
            "final_balance": round(equity_snapshots[-1].balance, 2)
            if equity_snapshots else round(initial_balance, 2),
            "final_equity": round(final_eq, 2),
            "total_return_pct": round(total_return, 2),
            "total_trades": total_trades,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": round(win_rate, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "realized_pnl": round(net_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "net_pnl": round(net_pnl + unrealized_pnl, 2),
            "profit_factor": profit_factor,
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
        }


# ---------------------------------------------------------------------------
#  PaperTradingEngine
# ---------------------------------------------------------------------------


class PaperTradingEngine:
    """Orchestrate paper trading simulation."""

    def __init__(self) -> None:
        self.wallet = VirtualWallet(INITIAL_BALANCE)
        self.orders: list[Order] = []
        self.positions: dict[str, VirtualPosition] = {}
        self.equity_history: list[EquitySnapshot] = []
        self.metrics: dict[str, Any] = {}
        self._load_state()

    # ------------------------------------------------------------------
    #  State persistence (cross-cycle)
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Restore wallet, orders, positions, and equity history.

        If ``STATE_PATH`` does not exist the engine starts fresh with
        initial balance.
        """
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        self.wallet.balance = state.get("balance", INITIAL_BALANCE)
        self.wallet.margin_used = state.get("margin_used", 0.0)
        self.orders = [Order(**o) for o in state.get("orders", [])]
        self.positions = {
            sym: VirtualPosition(**vp)
            for sym, vp in state.get("positions", {}).items()
        }
        self.equity_history = [
            EquitySnapshot(**s) for s in state.get("equity_history", [])
        ]

    # ------------------------------------------------------------------
    #  Telegram notification helper
    # ------------------------------------------------------------------

    def _notify_buy(self, plan: dict, fill_price: float, order_id: str) -> None:
        """Send BUY OPENED Telegram notification with full details."""
        try:
            from bot.telegram import TelegramNotifier
            import bot.config as bot_cfg
            bot_cfg.CONFIG.update({
                "telegram_enabled": bool(os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"),
                "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),
                "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
                "telegram_timeout": int(os.getenv("TELEGRAM_TIMEOUT", "10")),
                "telegram_retry": int(os.getenv("TELEGRAM_RETRY", "3")),
            })

            from telegram.formatter import fmt_price as fp
            from telegram.ui import (
                header, SEPARATOR, wib_now, confidence_bar,
                progress_bar, ai_insight, build_message,
            )

            symbol = plan["symbol"]
            entry = fill_price
            curr = plan.get("current_price", fill_price)
            sl = plan.get("stop_loss", 0)
            tp1 = plan.get("tp1", 0)
            tp2 = plan.get("tp2", 0)
            tp3 = plan.get("tp3", 0)
            pos_size = plan.get("position_size_usdt", 0)
            confidence = plan.get("confidence", plan.get("probability", 0))
            signal = plan.get("recommendation", "BUY")
            reasons = plan.get("reasons", ["Paper trade executed"])

            exchange = str(os.getenv("EXCHANGE", "binance"))
            timeframe = str(os.getenv("TIMEFRAME", "1h"))

            trend = ""
            try:
                with open("data/scanner_results.json") as f:
                    sc_data = json.load(f)
                for p in sc_data.get("pairs", []):
                    if p.get("symbol") == symbol:
                        trend = p.get("trend_alignment", "")
                        break
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            text = build_message(
                header(),
                f"🟢 *BUY OPENED*\n{symbol} • {exchange} • {timeframe}",
                f"{SEPARATOR}\n"
                f"💰 Entry\n{fp(entry)}\n\n"
                f"📍 Current\n{fp(curr)}",
                f"{SEPARATOR}\n"
                f"🛑 Stop Loss\n{fp(sl)}\n\n"
                f"🎯 Take Profit\n{fp(tp1)}",
                f"{SEPARATOR}\n"
                f"🧠 *AI Insight*\n"
                f"{ai_insight(signal, reasons, trend, confidence, is_buy=True)}",
                f"{SEPARATOR}\n"
                f"⭐ Confidence\n{confidence_bar(confidence)}\n\n"
                f"🕐 {wib_now()}",
            )
            notifier = TelegramNotifier()
            notifier.send(text)
        except Exception as exc:
            logging.getLogger("ZetBot").warning(
                "Failed to send BUY notification: %s", exc
            )

    def _notify_close(
        self,
        symbol: str,
        exit_price: float,
        total_pnl: float,
        balance: float,
        exit_reason: str,
        holding_time: timedelta,
        entry_price: float,
        cost_basis: float = 0.0,
    ) -> None:
        """Send trade closed Telegram notification."""
        try:
            from bot.telegram import TelegramNotifier
            import bot.config as bot_cfg
            bot_cfg.CONFIG.update({
                "telegram_enabled": bool(os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"),
                "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),
                "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
                "telegram_timeout": int(os.getenv("TELEGRAM_TIMEOUT", "10")),
                "telegram_retry": int(os.getenv("TELEGRAM_RETRY", "3")),
            })

            from telegram.formatter import fmt_price as fp, fmt_holding
            from telegram.ui import (
                header, SEPARATOR, wib_now, pnl_emoji,
                ai_insight, build_message,
            )

            notifier = TelegramNotifier()
            pnl_pct = (total_pnl / cost_basis * 100) if cost_basis > 0 else 0.0

            if entry_price > 0 and exit_price > 0:
                roi_pct = ((exit_price - entry_price) / entry_price * 100)
            else:
                roi_pct = 0.0

            emoji_map = {
                "Take Profit": "🟢",
                "Stop Loss": "🔴",
                "Strategy Exit": "⚪",
            }
            reason_emoji = emoji_map.get(exit_reason, "❓")
            holding_str = fmt_holding(holding_time.total_seconds())

            insight = ai_insight(reasons=[exit_reason], is_buy=False)

            text = build_message(
                header(),
                f"🔴 *POSITION CLOSED*\n{symbol}",
                f"{SEPARATOR}\n"
                f"💰 Entry\n{fp(entry_price)}\n\n"
                f"🚪 Exit\n{fp(exit_price)}",
                f"{SEPARATOR}\n"
                f"📈 Profit\n{pnl_emoji(total_pnl)} ${total_pnl:+,.2f} ({pnl_pct:+.2f}%)\n\n"
                f"🕒 Held\n{holding_str}",
                f"{SEPARATOR}\n"
                f"🧠 *AI Insight*\n{insight}",
                f"{SEPARATOR}\n"
                f"💹 Balance\n${balance:,.2f}",
            )
            notifier.send(text)
        except Exception as exc:
            logging.getLogger("ZetBot").warning(
                "Failed to send close notification: %s", exc
            )

    @staticmethod
    def _holding_str(td: timedelta) -> str:
        from telegram.formatter import fmt_holding
        return fmt_holding(td.total_seconds())

    def _save_state(self) -> None:
        """Persist wallet, orders, positions, and equity history."""
        os.makedirs("data", exist_ok=True)
        state = {
            "version": 1,
            "balance": self.wallet.balance,
            "margin_used": self.wallet.margin_used,
            "orders": [asdict(o) for o in self.orders],
            "positions": {
                sym: asdict(vp)
                for sym, vp in self.positions.items()
            },
            "equity_history": [asdict(s) for s in self.equity_history],
        }
    
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)

        # Sync positions.json for Telegram/reporting
        positions_data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_positions": len(self.positions),
            "active_count": sum(
                1 for vp in self.positions.values()
                if vp.status in ("OPEN", "TRAILING", "BREAKEVEN", "PARTIAL")
            ),
            "closed_count": sum(
                1 for vp in self.positions.values()
                if vp.status in ("CLOSED", "STOPPED", "TIMEOUT")
            ),
            "positions": [
                asdict(vp)
                for vp in self.positions.values()
            ]
        }

        with open("data/positions.json", "w") as f:
            json.dump(positions_data, f, indent=2, default=str)

    def run(self) -> dict[str, Any]:
        """Full paper trading pipeline."""
        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — PAPER TRADING ENGINE")
        print(f"  {'=' * 78}")
        print(f"  Initial Balance : ${INITIAL_BALANCE:>8,.2f}")
        print(f"  Taker Fee       : {TAKER_FEE * 100:.2f}%")
        print(f"  Slippage        : {SLIPPAGE_BPS} bps")
        print()

        t0 = time.time()

        # 1. Load data
        print("  [1/5] Loading data … ", end="", flush=True)
        plans = self._load_plans(TRADE_PLAN_PATH)
        pos_map = self._load_positions(POSITIONS_PATH)
        print(f"{len(plans)} plans, {len(pos_map)} position states")

        if not plans:
            self._save_state()
            print("  No READY plans.  Exiting.")
            return {}

        # 2. Execute orders (best confidence first)
        print("  [2/5] Executing orders …", flush=True)
        plans.sort(key=lambda p: p.get("confidence", 0), reverse=True)
        reconciled_symbols: set[str] = set()
        for plan in plans:
            symbol = plan["symbol"]
            # Skip re-execution for positions already open from prior runs
            vp = self.positions.get(symbol)
            if vp is not None and vp.status == "OPEN":
                continue
            order = self._execute_plan(plan,
            pos_map.get(symbol))

            if order is not None:
                self.equity_history.append(
                    self.wallet.snapshot(
            position_value=self._total_position_value(),
            unrealized_pnl_value=self._total_unrealized_pnl(),
                )
            )

        # 3. Reconcile with positions
        print("  [3/5] Reconciling positions …", flush=True)
        for plan in plans:
            symbol = plan["symbol"]
            self._reconcile(plan, pos_map.get(symbol))
            reconciled_symbols.add(symbol)
            self.equity_history.append(
                self.wallet.snapshot(
                    position_value=self._total_position_value(),
                    unrealized_pnl_value=self._total_unrealized_pnl(),
                )
            )

        # 3b. Reconcile open positions not in current plans
        for symbol, vp in list(self.positions.items()):
            if vp.status != "OPEN" or symbol in reconciled_symbols:
                continue
            pos_state = pos_map.get(symbol)
            if pos_state is not None:
                self._reconcile({"symbol": symbol}, pos_state)
                self.equity_history.append(
                    self.wallet.snapshot(
                        position_value=self._total_position_value(),
                        unrealized_pnl_value=self._total_unrealized_pnl(),
                    )
                )

        # 4. Compute metrics
        print("  [4/5] Computing metrics …", flush=True)
        self.metrics = MetricsCalculator.compute(
            self.orders, self.equity_history, INITIAL_BALANCE,
            unrealized_pnl=self._total_unrealized_pnl(),
        )

        elapsed = time.time() - t0

        # 5. Report
        print("  [5/5] Generating report …", flush=True)
        self._print_summary(elapsed)

        self._save_state()

        return self.metrics

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    def _load_plans(self, path: str) -> list[dict]:
        with open(path) as f:
            data = json.load(f)
        return [
            p for p in data.get("plans", [])
            if p.get("status") == "READY"
        ]

    def _load_positions(self, path: str) -> dict[str, dict]:
        with open(path) as f:
            data = json.load(f)
        return {p["symbol"]: p for p in data.get("positions", [])}

    def _execute_plan(self, plan: dict, pos_state: dict | None) -> Order | None:
        symbol = plan["symbol"]
        entry = plan["entry_price"]
        plan_qty = plan["quantity"]
        plan_val = plan["position_size_usdt"]

        now_ts = datetime.now(timezone.utc).isoformat()
        order_id = _next_order_id()

        plan_signal = plan.get("signal_time", "")

        vp = self.positions.get(symbol)


        if (
            vp is not None
            and vp.signal_time == plan_signal
        ):
            return None

        # --- Scale-down if position exceeds free balance ---
        unit_cost = ExecutionModel.buy(entry, 1.0)["total_cost"]
        max_affordable = self.wallet.free_balance / unit_cost if unit_cost > 0 else 0.0

        if plan_qty > max_affordable:
            if max_affordable < 1.0:
                order = Order(
                    id=order_id, symbol=symbol,
                    side="BUY", type="MARKET",
                    quantity=plan_qty, filled_quantity=0.0,
                    entry_price=entry, fill_price=0.0,
                    slippage=0.0, entry_fee=0.0,
                    exit_price=0.0, exit_fee=0.0,
                    total_cost=0.0, total_proceeds=0.0,
                    net_pnl=0.0, net_pnl_pct=0.0,
                    status="CANCELLED",
                    created_at=now_ts, filled_at="", closed_at="",
                )
                self.orders.append(order)
                return order

            qty = math.floor(max_affordable * 10000) / 10000
            print(f"    SCALED {symbol}: {plan_qty:.2f} -> {qty:.2f} units "
                  f"(free=${self.wallet.free_balance:,.2f})")
        else:
            qty = plan_qty

        # --- Execute buy with slippage + fee ---
        exec_result = ExecutionModel.buy(entry, qty)
        fill_price = exec_result["fill_price"]
        entry_fee = exec_result["fee"]
        total_cost = exec_result["total_cost"]

        self.wallet.deduct(total_cost)

        current_price = (
            pos_state["current_price"]
            if pos_state and pos_state.get("current_price")
            else entry
        )

        order = Order(
            id=order_id, symbol=symbol,
            side="BUY", type="MARKET",
            quantity=qty, filled_quantity=qty,
            entry_price=entry, fill_price=fill_price,
            slippage=exec_result["slippage"],
            entry_fee=entry_fee,
            exit_price=0.0, exit_fee=0.0,
            total_cost=total_cost, total_proceeds=0.0,
            net_pnl=0.0, net_pnl_pct=0.0,
            status="FILLED",
            created_at=now_ts, filled_at=now_ts, closed_at="",
        )
        self.orders.append(order)

        self.positions[symbol] = VirtualPosition(
            symbol=symbol, order_id=order_id,
            quantity=qty, remaining_qty=qty,
            entry_price=fill_price,
            current_price=current_price,
            unrealized_pnl=current_price * qty - total_cost,
            realized_pnl=0.0, total_pnl=0.0,
            cost_basis=total_cost,
            status="OPEN",
            opened_at=now_ts,
            signal_time=plan.get("signal_time", ""),
        )

        # Send BUY OPENED notification
        self._notify_buy(plan, fill_price, order_id)

        return order

    def _reconcile(self, plan: dict, pos_state: dict | None) -> None:
        symbol = plan["symbol"]
        vp = self.positions.get(symbol)
        if vp is None or vp.status != "OPEN":
            return
        if pos_state is None:
            return

        now_ts = datetime.now(timezone.utc).isoformat()
        current_price = pos_state.get("current_price", vp.entry_price)
        pos_status = pos_state.get("status", "OPEN")
        tp1_hit = pos_state.get("tp1_hit", False)
        tp2_hit = pos_state.get("tp2_hit", False)
        tp3_hit = pos_state.get("tp3_hit", False)
        stop_loss = pos_state.get("stop_loss", 0.0)

        total_qty = vp.quantity
        remaining_qty = vp.remaining_qty
        realized_pnl = vp.realized_pnl

        # --- Process each TP hit by fraction of our own tracked qty ---
        tp_config = [
            (tp1_hit, pos_state.get("tp1", 0.0), 0.30, "tp1_sold"),
            (tp2_hit, pos_state.get("tp2", 0.0), 0.30, "tp2_sold"),
            (tp3_hit, pos_state.get("tp3", 0.0), 0.40, "tp3_sold"),
        ]
        for hit, price, fraction, sold_attr in tp_config:
            if not hit:
                continue
            if getattr(vp, sold_attr, False):
                continue
            sell_qty = total_qty * fraction
            sell_qty = min(sell_qty, remaining_qty)
            if sell_qty <= 0.0:
                continue

            sell_result = ExecutionModel.sell(price, sell_qty)
            self.wallet.add(sell_result["total_proceeds"])

            cost_part = vp.cost_basis * (sell_qty / total_qty)
            pnl = sell_result["total_proceeds"] - cost_part
            realized_pnl += pnl

            self.orders.append(Order(
                id=_next_order_id(), symbol=symbol,
                side="SELL", type="MARKET",
                quantity=sell_qty, filled_quantity=sell_qty,
                entry_price=vp.entry_price,
                fill_price=sell_result["fill_price"],
                slippage=sell_result["slippage"],
                entry_fee=0.0,
                exit_price=sell_result["fill_price"],
                exit_fee=sell_result["fee"],
                total_cost=0.0,
                total_proceeds=sell_result["total_proceeds"],
                net_pnl=round(pnl, 2),
                net_pnl_pct=round(
                    (sell_result["total_proceeds"] / cost_part - 1) * 100, 2
                ) if cost_part > 0 else 0.0,
                status="CLOSED",
                created_at=vp.opened_at, filled_at=vp.opened_at, closed_at=now_ts,
                exit_reason="Take Profit",
            ))

            setattr(vp, sold_attr, True)
            remaining_qty -= sell_qty

        # --- Full close (STOPPED / CLOSED / TIMEOUT) ---
        if remaining_qty <= 0:
            vp.status = "CLOSED"
            vp.remaining_qty = 0.0
            vp.unrealized_pnl = 0.0
            vp.realized_pnl = round(realized_pnl, 2)
            vp.total_pnl = round(realized_pnl, 2)

            exit_reason = self._resolve_exit_reason(
                "Take Profit", pos_status, tp1_hit, tp2_hit, tp3_hit,
                vp.total_pnl,
            )
            holding_time = self._calc_holding_time(vp.opened_at, now_ts)
            if not vp.closure_notified:
                self._notify_close(
                    symbol=vp.symbol,
                    exit_price=vp.current_price,
                    total_pnl=vp.total_pnl,
                    balance=self.wallet.balance,
                    exit_reason=exit_reason,
                    holding_time=holding_time,
                    entry_price=vp.entry_price,
                    cost_basis=vp.cost_basis,
                )
                vp.closure_notified = True
            return

        if pos_status in ("CLOSED", "STOPPED", "TIMEOUT") and remaining_qty > 0.0:
            exit_price = current_price
            if pos_status == "STOPPED":
                exit_price = pos_state.get("current_stop", stop_loss)
                if exit_price <= 0.0:
                    exit_price = current_price

            sell_result = ExecutionModel.sell(exit_price, remaining_qty)
            self.wallet.add(sell_result["total_proceeds"])

            cost_part = vp.cost_basis * (remaining_qty / total_qty)
            close_pnl = sell_result["total_proceeds"] - cost_part
            realized_pnl += close_pnl

            # Determine exit reason from position status
            if pos_status == "STOPPED":
                raw_reason = "Stop Loss"
            elif pos_status == "TIMEOUT":
                raw_reason = "Strategy Exit"
            elif tp3_hit or tp2_hit or tp1_hit:
                raw_reason = "Take Profit"
            else:
                raw_reason = "Strategy Exit"

            exit_reason = self._resolve_exit_reason(
                raw_reason, pos_status, tp1_hit, tp2_hit, tp3_hit,
                round(realized_pnl, 2),
            )

            self.orders.append(Order(
                id=_next_order_id(), symbol=symbol,
                side="SELL", type="MARKET",
                quantity=remaining_qty, filled_quantity=remaining_qty,
                entry_price=vp.entry_price,
                fill_price=sell_result["fill_price"],
                slippage=sell_result["slippage"],
                entry_fee=0.0,
                exit_price=sell_result["fill_price"],
                exit_fee=sell_result["fee"],
                total_cost=0.0,
                total_proceeds=sell_result["total_proceeds"],
                net_pnl=round(close_pnl, 2),
                net_pnl_pct=round(
                    (sell_result["total_proceeds"] / cost_part - 1) * 100, 2
                ) if cost_part > 0 else 0.0,
                status="CLOSED",
                created_at=vp.opened_at, filled_at=vp.opened_at, closed_at=now_ts,
                exit_reason=exit_reason,
            ))

            vp.status = "CLOSED"
            vp.remaining_qty = 0.0
            vp.current_price = exit_price
            vp.realized_pnl = round(realized_pnl, 2)
            vp.unrealized_pnl = 0.0
            vp.total_pnl = round(realized_pnl, 2)


            holding_time = self._calc_holding_time(vp.opened_at, now_ts)
            if not vp.closure_notified:
                self._notify_close(
                    symbol=vp.symbol,
                    exit_price=exit_price,
                    total_pnl=vp.total_pnl,
                    balance=self.wallet.balance,
                    exit_reason=exit_reason,
                    holding_time=holding_time,
                    entry_price=vp.entry_price,
                    cost_basis=vp.cost_basis,
                )
                vp.closure_notified = True
        else:
            cost_remaining = vp.cost_basis * (remaining_qty / total_qty) \
                if total_qty > 0 else 0.0
            vp.remaining_qty = remaining_qty
            vp.current_price = current_price
            vp.unrealized_pnl = round(
                current_price * remaining_qty - cost_remaining, 2
            )
            vp.realized_pnl = round(realized_pnl, 2)
            vp.total_pnl = round(vp.realized_pnl + vp.unrealized_pnl, 2)

    @staticmethod
    def _resolve_exit_reason(
        raw_reason: str,
        pos_status: str,
        tp1_hit: bool,
        tp2_hit: bool,
        tp3_hit: bool,
        total_pnl: float,
    ) -> str:
        """Reclassify exit reason so 'Take Profit' never appears with negative PnL."""
        if raw_reason == "Take Profit" and total_pnl < 0:
            if pos_status == "STOPPED":
                return "Stop Loss"
            return "Strategy Exit"
        return raw_reason

    def _calc_holding_time(self, opened_at: str, closed_at: str) -> timedelta:
        """Calculate holding duration between two ISO timestamps."""
        try:
            open_dt = datetime.fromisoformat(opened_at)
            close_dt = datetime.fromisoformat(closed_at)
            return close_dt - open_dt
        except (ValueError, TypeError):
            return timedelta()

    def _total_position_value(self) -> float:
        return sum(p.current_price * p.remaining_qty for p in self.positions.values()
                   if p.status == "OPEN")

    def _total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values()
                   if p.status == "OPEN")

    # ------------------------------------------------------------------
    #  Report
    # ------------------------------------------------------------------

    def _print_summary(self, elapsed: float) -> None:
        m = self.metrics
        open_pos = sum(1 for p in self.positions.values() if p.status == "OPEN")
        closed_pos = sum(1 for p in self.positions.values() if p.status == "CLOSED")
        filled = [o for o in self.orders if o.status in ("FILLED", "CLOSED")]
        cancelled = [o for o in self.orders if o.status == "CANCELLED"]
        total_trades = m.get("total_trades", 0)

        print()
        print(f"  {'=' * 78}")
        print(f"  PAPER TRADING ENGINE — RESULTS")
        print(f"  {'=' * 78}")
        print(f"  USDT Balance     : ${m.get('final_balance', 0):>8,.2f}")
        print(f"  Equity           : ${m.get('final_equity', 0):>8,.2f}")
        print(f"  Realized PnL     : ${m.get('realized_pnl', 0):>+8,.2f}")
        print(f"  Unrealized PnL   : ${m.get('unrealized_pnl', 0):>+8,.2f}")
        print(f"  Net PnL          : ${m.get('net_pnl', 0):>+8,.2f}")
        print(f"  Return           : {m.get('total_return_pct', 0):>+7.2f}%")
        print(f"  Open Positions   : {open_pos}")
        print(f"  Closed Positions : {closed_pos}")
        print(f"  Filled Orders    : {len(filled)}")
        if cancelled:
            print(f"  Cancelled Orders : {len(cancelled)}")
        print(f"  Win Rate         : {m.get('win_rate', 0):.1f}%")
        print(f"  Profit Factor    : {_fmt_pf(m.get('profit_factor', 0))}")
        print(f"  Max Drawdown     : ${m.get('max_drawdown', 0):>8,.2f} "
              f"({m.get('max_drawdown_pct', 0):.2f}%)")
        print(f"  Execution Time   : {elapsed:.2f}s")
        print(f"  {'=' * 78}")
        print()

        if cancelled:
            print(f"  CANCELLED ORDERS:")
            for o in cancelled:
                print(f"    {o.symbol:>12s}  "
                      f"qty={o.quantity:>10.2f}  "
                      f"entry={o.entry_price:.6f}")
            print()

        # Trade history (last 5 only)
        closed = [o for o in self.orders if o.status == "CLOSED"]

        if closed:
            print(f"  LAST 5 CLOSED TRADES ({len(closed)} total):")

            hdr = (
        f"  {'#':>4s} {'Pair':>12s} {'Side':>5s} "
        f"{'PnL $':>10s} {'PnL%':>7s} "
        f"{'Entry':>10s} {'Exit':>10s} {'Fees':>7s}"
        )

            print(hdr)
            print(f"  {'-' * (len(hdr) - 2)}")

            last_closed = closed[-5:]
            start_no = len(closed) - len(last_closed) + 1

            for i, o in enumerate(last_closed, start_no):
                tot_fee = o.entry_fee + o.exit_fee

                print(
                f"  {i:4d} {o.symbol:>12s} {o.side:>5s} "
                f"{o.net_pnl:>+10.2f} {o.net_pnl_pct:>+7.2f} "
                f"{o.entry_price:>10.4f} {o.exit_price:>10.4f} "
                f"{tot_fee:>7.4f}"
            )

            print()
        else:
            print("  No closed trades.")
            print()


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


# -------------------------------------------------------------------
#  File export
# -------------------------------------------------------------------


class PaperExport:
    """Write paper trading outputs."""

    @staticmethod
    def orders_csv(orders: list[Order], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "id", "symbol", "side", "type",
            "quantity", "filled_quantity",
            "entry_price", "fill_price", "slippage", "entry_fee",
            "exit_price", "exit_fee",
            "total_cost", "total_proceeds",
            "net_pnl", "net_pnl_pct",
            "status", "created_at", "filled_at", "closed_at",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for o in orders:
                row = asdict(o)
                w.writerow({k: row[k] for k in fields})
        print(f"  Orders CSV     : {path}")

    @staticmethod
    def orders_json(orders: list[Order], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open_cnt = sum(1 for o in orders if o.status in ("NEW", "FILLED", "PARTIALLY_FILLED"))
        closed_cnt = sum(1 for o in orders if o.status == "CLOSED")
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_orders": len(orders),
            "open_orders": open_cnt,
            "closed_orders": closed_cnt,
            "orders": [asdict(o) for o in orders],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Orders JSON    : {path}")

    @staticmethod
    def balance_json(
        metrics: dict, equity_history: list[EquitySnapshot], path: str,
    ) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = dict(metrics)
        data["generated"] = datetime.now(timezone.utc).isoformat()
        data["equity_history"] = [asdict(s) for s in equity_history]
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Balance JSON   : {path}")

    @staticmethod
    def trade_history_csv(orders: list[Order], path: str) -> None:
        """Write only CLOSED orders as trade history."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        closed = [o for o in orders if o.status == "CLOSED"]
        fields = [
            "id", "symbol", "side",
            "quantity", "entry_price", "fill_price",
            "exit_price", "entry_fee", "exit_fee",
            "net_pnl", "net_pnl_pct",
            "created_at", "filled_at", "closed_at",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for o in closed:
                row = asdict(o)
                w.writerow({k: row[k] for k in fields})
        print(f"  Trade History   : {path}")


# -------------------------------------------------------------------
#  Main
# -------------------------------------------------------------------


def main() -> None:
    engine = PaperTradingEngine()
    metrics = engine.run()

    if not metrics:
        return


    PaperExport.orders_csv(engine.orders, "data/paper_orders.csv")
    PaperExport.orders_json(engine.orders, "data/paper_orders.json")
    PaperExport.balance_json(metrics, engine.equity_history, "data/paper_balance.json")
    PaperExport.trade_history_csv(engine.orders, "data/paper_trade_history.csv")

    print(f"\n  Completed at  : {datetime.now(timezone.utc).isoformat()}")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    main()
