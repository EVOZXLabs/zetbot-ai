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

    Tracks a single open position with virtual balance, stop loss,
    and take profit levels computed from config.
    """

    def __init__(
        self,
        initial_balance: float = _INITIAL_BALANCE,
    ) -> None:
        self._balance: float = initial_balance
        self._position: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
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

    def reset(self) -> None:
        """Clear the position and restore the initial balance."""
        self._position = None
        self._balance = _INITIAL_BALANCE
        logger.info("Paper trader reset")
