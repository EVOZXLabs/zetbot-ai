"""
Paper Trading Module

ZetBot AI
"""

import logging
from datetime import datetime, timezone
from typing import Any

from bot.config import CONFIG

logger = logging.getLogger("ZetBot")

_INITIAL_BALANCE = 10_000.0


class PaperTrader:
    """Virtual paper trader — no real orders or exchange interaction.

    Supports the full trade lifecycle:

    - Open a long position via :meth:`open_position`.
    - Close it via :meth:`close_position` (take-profit, stop-loss,
      strategy exit, or manual close).
    - Inspect the current position, trade history, and P&L stats.
    """

    def __init__(
        self,
        initial_balance: float = _INITIAL_BALANCE,
    ) -> None:
        self._balance: float = initial_balance
        self._position: dict[str, Any] | None = None
        self._trades: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API – position lifecycle
    # ------------------------------------------------------------------

    def open_position(
        self,
        entry_price: float,
        symbol: str,
        timeframe: str,
        reasons: list[str],
        atr_pct: float | None = None,
        ema200: float | None = None,
    ) -> dict[str, Any] | None:
        """Open a virtual long position.

        Position size is always equity-relative (``% of current
        balance``, not a fixed dollar amount), so results scale
        automatically whether the account is $10 or $100,000.

        Stop-loss and take-profit are volatility-aware: when ``atr_pct``
        is supplied, the stop distance is ``atr_pct * ATR_STOP_MULTIPLIER``
        and the target is ``stop_distance * RISK_REWARD_RATIO`` — both
        widen or tighten automatically with current market volatility.
        If ``atr_pct`` is unavailable (e.g. not enough candles yet),
        this falls back to the fixed ``stop_loss`` / ``take_profit`` %
        from config.

        Args:
            entry_price: Price at which the position is opened.
            symbol: Trading pair (e.g. ``"BTC/USDT"``).
            timeframe: Candle timeframe (e.g. ``"1h"``).
            reasons: Strategy reasons that triggered the BUY signal.
            atr_pct: Current ATR expressed as a % of price (e.g. ``2.3``
                for 2.3%). Pass ``None`` to force the fixed-% fallback.

        Returns:
            The position dict, or ``None`` if a position is already
            open, or if the sized position falls below the exchange's
            minimum notional (``min_position_usd``) for this equity.
        """
        if self._position is not None:
            logger.warning(
                "Paper BUY rejected — position already open",
            )
            return None

        position_size_pct = float(CONFIG.get("position_size", 10))
        atr_multiplier = float(CONFIG.get("atr_stop_multiplier", 1.5))
        risk_reward = float(CONFIG.get("risk_reward_ratio", 2.0))
        min_position_usd = float(CONFIG.get("min_position_usd", 5.0))

        # ── Dynamic SL/TP via risk_manager (same as live pipeline) ──
        if atr_pct is not None and atr_pct > 0 and ema200 is not None and ema200 > 0:
            from scripts.risk_manager import StopLossCalculator, TakeProfitCalculator
            stop_loss_price, stop_method = StopLossCalculator.safest(
                entry_price, atr_pct, ema200,
            )
            tp_prices = TakeProfitCalculator.calculate(entry_price, stop_loss_price)
            tp1 = tp_prices[0] if tp_prices else entry_price
            tp2 = tp_prices[1] if len(tp_prices) > 1 else 0.0
            tp3 = tp_prices[2] if len(tp_prices) > 2 else 0.0
            stop_loss_pct = (entry_price - stop_loss_price) / entry_price * 100.0
            sizing_method = stop_method
            take_profit_price = tp1
        else:
            # Fallback to fixed % (legacy — EMA200 or ATR unavailable)
            stop_loss_pct = float(CONFIG.get("stop_loss", 1.5))
            take_profit_pct = float(CONFIG.get("take_profit", 2.5))
            sizing_method = "Fixed%"
            stop_loss_price = entry_price * (1.0 - stop_loss_pct / 100.0)
            take_profit_price = entry_price * (1.0 + take_profit_pct / 100.0)
            tp1, tp2, tp3 = take_profit_price, 0.0, 0.0

        position_value = self._balance * (position_size_pct / 100.0)

        if position_value < min_position_usd:
            logger.info(
                "Paper BUY rejected — position $%.2f below min notional "
                "$%.2f for balance=$%.2f (%.1f%% sizing). Account too "
                "small for current position_size%% setting.",
                position_value, min_position_usd, self._balance,
                position_size_pct,
            )
            return None

        quantity = position_value / entry_price

        self._position = {
            "entry_time": datetime.now(timezone.utc),
            "entry_price": entry_price,
            "quantity": quantity,
            "balance_before": self._balance,
            "position_size_percent": position_size_pct,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "stop_method": sizing_method,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": ((take_profit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0,
            "status": "OPEN",
            "symbol": symbol,
            "timeframe": timeframe,
        }

        reasons_str = " | ".join(reasons)
        logger.info(
            "Paper BUY opened | "
            "Entry=%.2f SL=%.2f (-%.2f%%, %s) TP=%.2f (+%.2f%%) "
            "Size=%s%.2f%% (%.4f %s) | %s",
            entry_price,
            stop_loss_price, stop_loss_pct, sizing_method,
            take_profit_price, (
                (take_profit_price - entry_price) / entry_price * 100.0
                if entry_price > 0 else 0.0
            ),
            f"${position_value:,.2f} / ",
            position_size_pct,
            quantity,
            symbol.split("/")[0],
            reasons_str,
        )

        return dict(self._position)

    def close_position(
        self,
        exit_price: float,
        exit_reason: str,
    ) -> dict[str, Any] | None:
        """Close the currently open position.

        Calculates realised P&L, updates the virtual balance, and
        stores the completed trade in the history log.

        Args:
            exit_price: Price at which the position is closed.
            exit_reason: Why the position was closed.  One of
                ``"Take Profit"``, ``"Stop Loss"``, ``"Strategy Exit"``,
                or ``"Manual Close"``.

        Returns:
            A dict with the closed position (including P&L fields),
            or ``None`` if no position is open.

        Raises:
            ValueError: If ``exit_price`` is not positive.
        """
        if self._position is None:
            logger.warning("Paper SELL rejected — no open position")
            return None

        if exit_price <= 0:
            msg = f"exit_price must be positive, got {exit_price}"
            raise ValueError(msg)

        pos = self._position

        gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        net_pnl = gross_pnl  # no fees in paper trading
        pnl_pct = ((exit_price / pos["entry_price"]) - 1.0) * 100.0

        balance_after = pos["balance_before"] + net_pnl
        self._balance = balance_after

        exit_time = datetime.now(timezone.utc)
        holding_time = exit_time - pos["entry_time"]

        closed = {
            "entry_time": pos["entry_time"],
            "exit_time": exit_time,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "quantity": pos["quantity"],
            "position_size_percent": pos["position_size_percent"],
            "stop_loss_price": pos["stop_loss_price"],
            "take_profit_price": pos["take_profit_price"],
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "holding_time": holding_time,
            "exit_reason": exit_reason,
            "balance_after": balance_after,
            "symbol": pos["symbol"],
            "timeframe": pos["timeframe"],
        }

        self._trades.append(closed)
        self._position = None

        logger.info(
            "Paper SELL closed | %s | "
            "Entry=%.2f Exit=%.2f PnL=%+.2f (%+.2f%%) "
            "Balance=%.2f | Held=%s",
            exit_reason,
            pos["entry_price"],
            exit_price,
            net_pnl,
            pnl_pct,
            balance_after,
            _format_timedelta(holding_time),
        )

        return dict(closed)

    # ------------------------------------------------------------------
    # Public API – position queries
    # ------------------------------------------------------------------

    def has_position(self) -> bool:
        """Check whether a paper position is currently open.

        Returns:
            ``True`` if a position exists and is active.
        """
        return self._position is not None

    def current_position(self) -> dict[str, Any] | None:
        """Return the current position, or ``None``.

        Returns:
            A copy of the position dict, or ``None``.
        """
        if self._position is None:
            return None
        return dict(self._position)

    # ------------------------------------------------------------------
    # Public API – trade history
    # ------------------------------------------------------------------

    def trade_history(self) -> list[dict[str, Any]]:
        """Return all completed trades.

        Returns:
            A list of closed-position dicts, oldest first.
        """
        return list(self._trades)

    def last_trade(self) -> dict[str, Any] | None:
        """Return the most recently closed trade, or ``None``.

        Returns:
            A closed-position dict, or ``None`` if no trades.
        """
        if not self._trades:
            return None
        return dict(self._trades[-1])

    def total_profit(self) -> float:
        """Sum of net P&L across all closed trades.

        Returns:
            Total realised profit in USDT.
        """
        return sum(t["net_pnl"] for t in self._trades)

    def win_count(self) -> int:
        """Number of profitable closed trades.

        Returns:
            Count of trades where ``net_pnl > 0``.
        """
        return sum(1 for t in self._trades if t["net_pnl"] > 0)

    def loss_count(self) -> int:
        """Number of unprofitable closed trades.

        Returns:
            Count of trades where ``net_pnl <= 0``.
        """
        return sum(1 for t in self._trades if t["net_pnl"] <= 0)

    # ------------------------------------------------------------------
    # Public API – state serialization
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Export current state for persistence.

        Returns:
            A dict with ``balance``, ``position``, and ``trades``.
        """
        return {
            "balance": self._balance,
            "position": dict(self._position) if self._position else None,
            "trades": list(self._trades),
        }

    def set_state(
        self,
        balance: float,
        position: dict[str, Any] | None,
        trades: list[dict[str, Any]],
    ) -> None:
        """Restore internal state from previously saved data.

        Args:
            balance: Virtual balance to restore.
            position: Open position dict, or ``None``.
            trades: Completed trades list.
        """
        self._balance = balance
        self._position = dict(position) if position else None
        self._trades = list(trades)
        logger.info(
            "Paper trader state restored — balance=%.2f position=%s trades=%d",
            self._balance,
            "YES" if self._position else "NO",
            len(self._trades),
        )

    # ------------------------------------------------------------------
    # Public API – lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear the position, trade history, and restore balance."""
        self._position = None
        self._trades.clear()
        self._balance = _INITIAL_BALANCE
        logger.info("Paper trader reset")


def _format_timedelta(td: Any) -> str:
    total_sec = int(td.total_seconds())
    hours, remainder = divmod(total_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
