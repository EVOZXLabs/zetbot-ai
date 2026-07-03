"""
Indicator Engine Module

ZetBot AI
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("ZetBot")


class IndicatorEngine:
    """Calculate technical indicators on OHLCV data.

    All methods are static. Indicators are computed using pandas
    vectorised operations. No external TA library is used.

    Currently implemented:
        - EMA (Exponential Moving Average)
    """

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        """Calculate Exponential Moving Average.

        Uses the standard formula::

            alpha = 2 / (period + 1)
            EMA_t = alpha * price_t + (1 - alpha) * EMA_{t-1}

        The seed value is the first element of the series, matching
        TradingView's ``ta.ema`` behaviour within floating-point
        tolerance.

        Args:
            series: Price series (typically ``close``).
            period: Lookback period. Must be >= 2.

        Returns:
            pd.Series: EMA values with the same index as the input.
                The first ``period - 1`` values are valid (calculated
                from the seed) but are not stable until ``period``
                data points are available.

        Raises:
            ValueError: If ``series`` is empty, contains NaN, or
                ``period`` is less than 2.
        """
        if not isinstance(period, int) or period < 2:
            msg = f"period must be an integer >= 2, got {period}"
            raise ValueError(msg)

        if series.empty:
            msg = "series is empty, cannot calculate EMA"
            raise ValueError(msg)

        if series.isnull().any():
            count = int(series.isnull().sum())
            msg = f"series contains {count} NaN value(s), cannot calculate EMA"
            raise ValueError(msg)

        alpha = 2.0 / (period + 1.0)
        ema_values = series.ewm(alpha=alpha, adjust=False).mean()

        ema_values.name = f"EMA_{period}"
        return ema_values

    @staticmethod
    def ema200(
        df: pd.DataFrame,
        column: str = "close",
    ) -> float:
        """Calculate the latest EMA200 value from a DataFrame.

        This is a convenience wrapper around ``ema()`` that extracts
        the most recent EMA(200) value for quick use in strategy
        conditions.

        Args:
            df: DataFrame containing at least a ``close`` column (or
                the column specified by ``column``).
            column: Name of the price column to use. Defaults to
                ``"close"``.

        Returns:
            float: The latest EMA200 value.

        Raises:
            ValueError: If the DataFrame is empty, the column does not
                exist, contains NaN, or has fewer than 2 rows.

        Examples:
            >>> df = md.fetch_ohlcv("BTC/USDT", "1h", 250)
            >>> ema = IndicatorEngine.ema200(df)
        """
        if df is None or df.empty:
            msg = "DataFrame is empty, cannot calculate EMA200"
            raise ValueError(msg)

        if column not in df.columns:
            msg = (
                f"Column '{column}' not found in DataFrame. "
                f"Available: {list(df.columns)}"
            )
            raise ValueError(msg)

        series = df[column]
        ema_series = IndicatorEngine.ema(series, period=200)
        latest = ema_series.iloc[-1]

        result = float(latest)
        logger.info("EMA200 = %.2f (based on %d data points)", result, len(df))
        return result
