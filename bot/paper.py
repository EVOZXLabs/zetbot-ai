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
    ) -> dict[str, Any] | None:
        """Open a virtual long position.

        Args:
            entry_price: Price at which the position is opened.
            symbol: Trading pair (e.g. ``"BTC/USDT"``).
            timeframe: Candle timeframe (e.g. ``"1h"``).
            reasons: Strategy reasons that triggered the BUY signal.

        Returns:
            The position dict, or ``None`` if a position is already open.
        """
        if self._position is not None:
            logger.warning(
                "Paper BUY rejected — position already open",
            )
            return None

        position_size_pct = float(CONFIG.get("position_size", 10))
        stop_loss_pct = float(CONFIG.get("stop_loss", 1.5))
        take_profit_pct = float(CONFIG.get("take_profit", 2.5))

        position_value = self._balance * (position_size_pct / 100.0)
        quantity = position_value / entry_price

        stop_loss_price = entry_price * (1.0 - stop_loss_pct / 100.0)
        take_profit_price = entry_price * (1.0 + take_profit_pct / 100.0)

        self._position = {
            "entry_time": datetime.now(timezone.utc),
            "entry_price": entry_price,
            "quantity": quantity,
            "balance_before": self._balance,
            "position_size_percent": position_size_pct,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "status": "OPEN",
            "symbol": symbol,
            "timeframe": timeframe,
        }

        reasons_str = " | ".join(reasons)
        logger.info(
            "Paper BUY opened | "
            "Entry=%.2f SL=%.2f TP=%.2f "
            "Size=%s%.2f%% (%.4f %s) | %s",
            entry_price,
            stop_loss_price,
            take_profit_price,
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
