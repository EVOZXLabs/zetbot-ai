"""
Market State Detector Module

ZetBot AI
"""

import logging
from typing import Optional

import pandas as pd

from bot.indicators import IndicatorEngine

logger = logging.getLogger("ZetBot")

TRENDING = "TRENDING"
SIDEWAYS = "SIDEWAYS"


class MarketStateDetector:
    """Detect whether the market is trending or sideways.

    Combines ADX trend strength, ATR volatility compression, and
    price range compression to classify market state.

    All thresholds are configurable.
    """

    def __init__(
        self,
        adx_threshold: float = 25.0,
        atr_multiplier: float = 0.5,
        volatility_period: int = 14,
        compression_lookback: int = 20,
        compression_ratio: float = 0.3,
    ) -> None:
        """Initialise the detector.

        Args:
            adx_threshold: ADX >= this value is considered trending.
            atr_multiplier: Ratio of short/long ATR below this
                signals compression.
            volatility_period: Period for short-term ATR.
            compression_lookback: Candles for short-term price range.
            compression_ratio: Ratio of short/long price range below
                this signals compression.
        """
        self.adx_threshold = adx_threshold
        self.atr_multiplier = atr_multiplier
        self.volatility_period = volatility_period
        self.compression_lookback = compression_lookback
        self.compression_ratio = compression_ratio

    def _check_adx(self, df: pd.DataFrame) -> Optional[str]:
        """Check ADX trend strength.

        Args:
            df: OHLC DataFrame.

        Returns:
            ``"TRENDING"`` if ADX >= threshold, else ``None``.
        """
        adx_val = IndicatorEngine.adx(df, period=14)
        if adx_val >= self.adx_threshold:
            logger.info(
                "Market state -> TRENDING (ADX=%.2f >= %.1f)",
                adx_val, self.adx_threshold,
            )
            return TRENDING
        return None

    def _check_atr_compression(self, df: pd.DataFrame) -> bool:
        """Check if ATR shows volatility compression.

        Compares short-term ATR to a longer-term ATR (3x period).

        Args:
            df: OHLC DataFrame.

        Returns:
            ``True`` if compressed (short ATR / long ATR < multiplier).
        """
        long_period = self.volatility_period * 3
        min_rows = long_period + 1
        if len(df) < min_rows:
            return False

        atr_short = IndicatorEngine._atr_series(df, self.volatility_period)
        atr_long = IndicatorEngine._atr_series(df, long_period)

        current_short = float(atr_short.iloc[-1])
        current_long = float(atr_long.iloc[-1])

        if current_long == 0.0:
            return False

        ratio = current_short / current_long
        is_compressed = ratio < self.atr_multiplier

        if is_compressed:
            logger.info(
                "ATR compression: short=%.2f long=%.2f ratio=%.4f",
                current_short, current_long, ratio,
            )
        return is_compressed

    def _check_price_compression(self, df: pd.DataFrame) -> bool:
        """Check price range compression.

        Compares the high-low range over the short lookback to the
        range over a longer lookback (3x).

        Args:
            df: OHLC DataFrame.

        Returns:
            ``True`` if compressed (short range / long range < ratio).
        """
        long_lookback = self.compression_lookback * 3
        if len(df) < long_lookback:
            return False

        short_range = (
            df["high"].iloc[-self.compression_lookback:].max()
            - df["low"].iloc[-self.compression_lookback:].min()
        )
        long_range = (
            df["high"].iloc[-long_lookback:].max()
            - df["low"].iloc[-long_lookback:].min()
        )

        if long_range == 0.0:
            return False

        ratio = short_range / long_range
        is_compressed = ratio < self.compression_ratio

        if is_compressed:
            logger.info(
                "Price compression: short_range=%.2f "
                "long_range=%.2f ratio=%.4f",
                short_range, long_range, ratio,
            )
        return is_compressed

    def detect(self, df: pd.DataFrame) -> str:
        """Classify market state as trending or sideways.

        Decision logic:

        1. If ADX >= ``adx_threshold`` → **TRENDING**.
        2. If ATR shows volatility compression → **SIDEWAYS**.
        3. If price range is compressed → **SIDEWAYS**.
        4. Otherwise → **SIDEWAYS** (conservative — avoid weak
           trends unless ADX confirms strength).

        Args:
            df: DataFrame with ``high``, ``low``, ``close`` columns.

        Returns:
            ``"TRENDING"`` or ``"SIDEWAYS"``.
        """
        if df is None or df.empty:
            msg = "DataFrame is empty, cannot detect market state"
            raise ValueError(msg)

        missing = [c for c in ("high", "low", "close") if c not in df.columns]
        if missing:
            msg = (
                f"Missing required column(s) {missing} for "
                f"market state detection. Available: {list(df.columns)}"
            )
            raise ValueError(msg)

        # 1. Strong trend via ADX
        adx_result = self._check_adx(df)
        if adx_result is not None:
            return adx_result

        # 2. Volatility compression
        if self._check_atr_compression(df):
            logger.info("Market state -> SIDEWAYS (ATR compression)")
            return SIDEWAYS

        # 3. Price compression
        if self._check_price_compression(df):
            logger.info("Market state -> SIDEWAYS (price compression)")
            return SIDEWAYS

        # 4. Conservative default
        logger.info("Market state -> SIDEWAYS (default, no strong trend)")
        return SIDEWAYS
