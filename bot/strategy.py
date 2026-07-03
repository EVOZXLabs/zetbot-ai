"""
Strategy Engine Module

ZetBot AI
"""

import logging
from typing import Any

import pandas as pd

from bot.indicators import IndicatorEngine
from bot.state import MarketStateDetector, TRENDING

logger = logging.getLogger("ZetBot")

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"

_REQUIRED_COLS: tuple[str, str, str] = ("high", "low", "close")


class StrategyEngine:
    """Core strategy engine that combines indicators into trade signals.

    Evaluates market data and produces a structured signal:

    - **BUY**  — all conditions satisfied
    - **SELL** — any exit condition triggered
    - **HOLD** — no actionable signal

    The engine does **not** execute trades; it only generates signals.
    """

    def __init__(
        self,
        adx_threshold: float = 25.0,
        atr_multiplier: float = 0.5,
        volatility_period: int = 14,
        compression_lookback: int = 20,
        compression_ratio: float = 0.3,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
    ) -> None:
        """Initialise the strategy engine.

        Args:
            adx_threshold: ADX >= this value is trending.
            atr_multiplier: ATR ratio below this signals compression.
            volatility_period: Short-term ATR period.
            compression_lookback: Candles for short price range.
            compression_ratio: Range ratio below this is compressed.
            rsi_period: RSI lookback period.
            rsi_oversold: RSI below this is considered oversold.
        """
        self.adx_threshold = adx_threshold
        self.atr_multiplier = atr_multiplier
        self.volatility_period = volatility_period
        self.compression_lookback = compression_lookback
        self.compression_ratio = compression_ratio
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold

    def _validate(self, df: pd.DataFrame) -> None:
        """Validate the input DataFrame.

        Args:
            df: DataFrame to validate.

        Raises:
            ValueError: If DataFrame is None, empty, missing required
                columns, or contains NaN in critical columns.
        """
        if df is None or df.empty:
            msg = "DataFrame is empty, cannot evaluate strategy"
            raise ValueError(msg)

        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        if missing:
            msg = (
                f"Missing required column(s) {missing} for "
                f"strategy evaluation. Available: {list(df.columns)}"
            )
            raise ValueError(msg)

        for col in _REQUIRED_COLS:
            if df[col].isnull().any():
                count = int(df[col].isnull().sum())
                msg = (
                    f"Column '{col}' contains {count} NaN value(s), "
                    f"cannot evaluate strategy"
                )
                raise ValueError(msg)

    def _evaluate_buy(
        self,
        df: pd.DataFrame,
        has_position: bool,
    ) -> list[str]:
        """Check BUY conditions.

        Args:
            df: OHLC DataFrame.
            has_position: Whether a position is already open.

        Returns:
            List of satisfied reason strings.  If empty, no BUY.
        """
        if has_position:
            return []

        reasons: list[str] = []
        close = df["close"]
        price = float(close.iloc[-1])

        # 1. Price > EMA200
        ema200 = IndicatorEngine.ema200(df)
        if price > ema200:
            reasons.append("Price above EMA200")
        else:
            logger.info("BUY rejected: price %.2f <= EMA200 %.2f", price, ema200)
            return []

        # 2. RSI oversold
        rsi_val = IndicatorEngine.rsi(close, period=self.rsi_period)
        if rsi_val < self.rsi_oversold:
            reasons.append("RSI oversold")
        else:
            logger.info(
                "BUY rejected: RSI %.2f >= %.1f (not oversold)",
                rsi_val, self.rsi_oversold,
            )
            return []

        # 3. Market trending
        detector = MarketStateDetector(
            adx_threshold=self.adx_threshold,
            atr_multiplier=self.atr_multiplier,
            volatility_period=self.volatility_period,
            compression_lookback=self.compression_lookback,
            compression_ratio=self.compression_ratio,
        )
        state = detector.detect(df)
        if state == TRENDING:
            reasons.append("Market trending")
        else:
            logger.info("BUY rejected: market state is %s", state)
            return []

        if len(reasons) == 3:
            logger.info("BUY signal generated: %s", "; ".join(reasons))

        return reasons

    def _evaluate_sell(
        self,
        df: pd.DataFrame,
    ) -> list[str]:
        """Check SELL conditions.

        Args:
            df: OHLC DataFrame.

        Returns:
            List of satisfied reason strings.  If empty, no SELL.
        """
        reasons: list[str] = []
        close = df["close"]
        price = float(close.iloc[-1])

        # 1. Price < EMA200
        ema200 = IndicatorEngine.ema200(df)
        if price < ema200:
            reasons.append("Price below EMA200")
            logger.info("SELL signal: price %.2f below EMA200 %.2f", price, ema200)

        return reasons

    def evaluate(
        self,
        df: pd.DataFrame,
        has_position: bool = False,
    ) -> dict[str, Any]:
        """Evaluate the market and return a trading signal.

        Decision priority:

        1. If no position and all BUY conditions met → **BUY**.
        2. If any SELL condition met → **SELL**.
        3. Otherwise → **HOLD**.

        Args:
            df: DataFrame with ``open``, ``high``, ``low``, ``close``,
                ``volume`` columns.
            has_position: ``True`` if a position is currently open.

        Returns:
            dict with keys:

            - ``signal``: ``"BUY"`` | ``"SELL"`` | ``"HOLD"``
            - ``reason``: list of human-readable reason strings.

        Raises:
            ValueError: If the input data is invalid.
        """
        self._validate(df)

        # --- BUY check ---
        buy_reasons = self._evaluate_buy(df, has_position)
        if len(buy_reasons) == 3:
            return {"signal": BUY, "reason": buy_reasons}

        # --- SELL check ---
        sell_reasons = self._evaluate_sell(df)
        if sell_reasons:
            return {"signal": SELL, "reason": sell_reasons}

        # --- HOLD ---
        hold_reasons: list[str] = []
        if has_position:
            hold_reasons.append("Position open")
        else:
            hold_reasons.append("No signal triggered")
        return {"signal": HOLD, "reason": hold_reasons}
