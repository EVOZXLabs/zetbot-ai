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
        - RSI (Relative Strength Index)
    """

    @staticmethod
    def _validate_series(
        series: pd.Series,
        name: str,
    ) -> None:
        """Validate a price series for indicator calculation.

        Args:
            series: Input price series.
            name: Indicator name for error messages.

        Raises:
            ValueError: If series is empty or contains NaN.
        """
        if series.empty:
            msg = f"series is empty, cannot calculate {name}"
            raise ValueError(msg)

        if series.isnull().any():
            count = int(series.isnull().sum())
            msg = (
                f"series contains {count} NaN value(s), "
                f"cannot calculate {name}"
            )
            raise ValueError(msg)

    @staticmethod
    def _validate_period(period: int) -> int:
        """Validate and return a period integer.

        Args:
            period: Period value to validate.

        Returns:
            The period as an integer.

        Raises:
            ValueError: If period is not an integer or is less than 2.
        """
        if not isinstance(period, int) or period < 2:
            msg = f"period must be an integer >= 2, got {period}"
            raise ValueError(msg)
        return period

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
        period = IndicatorEngine._validate_period(period)
        IndicatorEngine._validate_series(series, "EMA")

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

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> float:
        """Calculate the latest Relative Strength Index.

        Uses Wilder's original RSI algorithm::

            diff      = close - previous_close
            gains     = max(diff, 0)
            losses    = max(-diff, 0)
            avg_gain  = Wilder_smooth(gains, period)
            avg_loss  = Wilder_smooth(losses, period)
            rs        = avg_gain / avg_loss
            rsi       = 100 - (100 / (1 + rs))

        The first average is a simple mean of the first ``period``
        gains/losses.  Subsequent values use Wilder's smoothing::

            value_t = (value_{t-1} * (period - 1) + input_t) / period

        which is equivalent to ``ewm(alpha=1/period, adjust=False)``
        seeded with the initial SMA.

        Args:
            series: Price series (typically ``close``).
            period: RSI lookback period. Must be >= 2. Default 14.

        Returns:
            float: The latest RSI value, rounded to 2 decimal places,
            in the range [0, 100].

        Raises:
            ValueError: If the input is empty, contains NaN, or has
                fewer than ``period + 1`` data points.
        """
        period = IndicatorEngine._validate_period(period)
        IndicatorEngine._validate_series(series, "RSI")

        if len(series) < period + 1:
            msg = (
                f"need at least {period + 1} data points for "
                f"RSI({period}), got {len(series)}"
            )
            raise ValueError(msg)

        diff = series.diff()
        gains = diff.clip(lower=0)
        losses = (-diff).clip(lower=0)

        first_avg_gain = gains.iloc[1:period + 1].mean()
        first_avg_loss = losses.iloc[1:period + 1].mean()

        g = gains.iloc[period:].copy()
        l = losses.iloc[period:].copy()
        g.iloc[0] = first_avg_gain
        l.iloc[0] = first_avg_loss

        alpha = 1.0 / period
        avg_gain_series = g.ewm(alpha=alpha, adjust=False).mean()
        avg_loss_series = l.ewm(alpha=alpha, adjust=False).mean()

        last_avg_gain = float(avg_gain_series.iloc[-1])
        last_avg_loss = float(avg_loss_series.iloc[-1])

        if last_avg_loss == 0.0 and last_avg_gain == 0.0:
            logger.info("RSI(%d) = 50.00 (constant prices, no movement)", period)
            return 50.0

        if last_avg_loss == 0.0:
            logger.info("RSI(%d) = 100.00 (no losses in window)", period)
            return 100.0

        if last_avg_gain == 0.0:
            logger.info("RSI(%d) = 0.00 (no gains in window)", period)
            return 0.0

        rs = last_avg_gain / last_avg_loss
        rsi_value = 100.0 - (100.0 / (1.0 + rs))
        result = round(float(rsi_value), 2)

        logger.info(
            "RSI(%d) = %.2f (avg_gain=%.4f, avg_loss=%.4f, rs=%.4f)",
            period, result, last_avg_gain, last_avg_loss, rs,
        )
        return result
