"""
Indicator Engine Module

ZetBot AI
"""

import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger("ZetBot")

_OHLC_COLS: tuple[str, str, str] = ("high", "low", "close")


class IndicatorEngine:
    """Calculate technical indicators on OHLCV data.

    All methods are static. Indicators are computed using pandas
    vectorised operations. No external TA library is used.

    Currently implemented:
        - EMA (Exponential Moving Average)
        - RSI (Relative Strength Index)
        - ADX (Average Directional Index) with +DI / -DI
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

    # ------------------------------------------------------------------ #
    #  ADX helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_ohlc_df(df: pd.DataFrame, name: str) -> None:
        """Validate a DataFrame has required OHLC columns.

        Args:
            df: DataFrame to validate.
            name: Indicator name for error messages.

        Raises:
            ValueError: If ``df`` is empty or missing high/low/close.
        """
        if df is None or df.empty:
            msg = f"DataFrame is empty, cannot calculate {name}"
            raise ValueError(msg)

        missing = [c for c in ("high", "low", "close") if c not in df.columns]
        if missing:
            msg = (
                f"Missing required column(s) {missing} for "
                f"{name}. Available: {list(df.columns)}"
            )
            raise ValueError(msg)

    @staticmethod
    def _wilder_smooth(raw: pd.Series, period: int) -> pd.Series:
        """Apply Wilder's smoothing (SMA seed then recursive).

        Args:
            raw: Input series whose first element is NaN
                (e.g. a ``diff()`` result).
            period: Smoothing period (>= 2).

        Returns:
            pd.Series: Smoothed values aligned to index ``period`` onward.
        """
        first_avg = raw.iloc[1:period + 1].mean()
        tail = raw.iloc[period:].copy()
        tail.iloc[0] = first_avg
        alpha = 1.0 / period
        return tail.ewm(alpha=alpha, adjust=False).mean()

    # ------------------------------------------------------------------ #
    #  ADX  (Average Directional Index)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _adx_components(
        df: pd.DataFrame,
        period: int,
    ) -> dict[str, float]:
        """Compute ADX, +DI, -DI for a given period.

        Args:
            df: DataFrame with ``high``, ``low``, ``close`` columns.
            period: Smoothing period (>= 2).

        Returns:
            dict with keys ``adx``, ``plus_di``, ``minus_di``.

        Raises:
            ValueError: If validation fails or data is insufficient.
        """
        period = IndicatorEngine._validate_period(period)
        IndicatorEngine._validate_ohlc_df(df, f"ADX({period})")

        min_rows = period * 2 + 1
        if len(df) < min_rows:
            msg = (
                f"need at least {min_rows} rows for ADX({period}), "
                f"got {len(df)}"
            )
            raise ValueError(msg)

        high = df["high"]
        low = df["low"]
        close = df["close"]

        # --- True Range ---
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # --- Directional Movement ---
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = pd.Series(0.0, index=df.index, dtype=float)
        minus_dm = pd.Series(0.0, index=df.index, dtype=float)

        up_pos = up_move > 0
        down_pos = down_move > 0
        up_gt_down = up_move > down_move
        down_gt_up = down_move > up_move

        plus_dm = plus_dm.where(~(up_pos & up_gt_down), up_move)
        minus_dm = minus_dm.where(~(down_pos & down_gt_up), down_move)

        # --- Wilder smooth TR -> ATR, DM -> smoothed DM ---
        atr_series = IndicatorEngine._wilder_smooth(tr, period)
        plus_dm_smooth = IndicatorEngine._wilder_smooth(plus_dm, period)
        minus_dm_smooth = IndicatorEngine._wilder_smooth(minus_dm, period)

        # --- +DI / -DI  (first valid at index ``period``) ---
        atr_last = float(atr_series.iloc[-1])
        plus_di_last = (
            100.0 * float(plus_dm_smooth.iloc[-1]) / atr_last
            if atr_last != 0.0 else 0.0
        )
        minus_di_last = (
            100.0 * float(minus_dm_smooth.iloc[-1]) / atr_last
            if atr_last != 0.0 else 0.0
        )

        # --- DX full series (for second Wilder smooth) ---
        atr_safe = atr_series.where(atr_series != 0.0)
        plus_di_full = 100.0 * plus_dm_smooth / atr_safe
        minus_di_full = 100.0 * minus_dm_smooth / atr_safe
        dx_full = (
            100.0 * (plus_di_full - minus_di_full).abs()
            / (plus_di_full + minus_di_full)
        )
        dx_full = dx_full.fillna(0.0)

        # --- ADX: Wilder smooth of DX ---
        first_adx = float(dx_full.iloc[:period].mean())
        adx_tail = dx_full.iloc[period - 1:].copy()
        adx_tail.iloc[0] = first_adx
        alpha = 1.0 / period
        adx_series = adx_tail.ewm(alpha=alpha, adjust=False).mean()
        latest_adx = float(adx_series.iloc[-1])

        return {
            "adx": round(latest_adx, 2),
            "plus_di": round(plus_di_last, 2),
            "minus_di": round(minus_di_last, 2),
        }

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest ADX (Average Directional Index).

        ADX measures trend strength, not direction.  Values > 25
        suggest a strong trend; < 20 suggests a ranging market.

        Args:
            df: DataFrame with ``high``, ``low``, ``close``.
            period: Smoothing period (default 14).

        Returns:
            Latest ADX value (0-100).

        Raises:
            ValueError: If data is invalid or insufficient.
        """
        comp = IndicatorEngine._adx_components(df, period)
        result = comp["adx"]
        logger.info(
            "ADX(%d) = %.2f (DI+ = %.2f, DI- = %.2f)",
            period, result, comp["plus_di"], comp["minus_di"],
        )
        return result

    @staticmethod
    def plus_di(df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest +DI (Positive Directional Indicator).

        Args:
            df: DataFrame with ``high``, ``low``, ``close``.
            period: Smoothing period (default 14).

        Returns:
            Latest +DI value (0-100).
        """
        comp = IndicatorEngine._adx_components(df, period)
        result = comp["plus_di"]
        logger.info("+DI(%d) = %.2f", period, result)
        return result

    @staticmethod
    def minus_di(df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest -DI (Negative Directional Indicator).

        Args:
            df: DataFrame with ``high``, ``low``, ``close``.
            period: Smoothing period (default 14).

        Returns:
            Latest -DI value (0-100).
        """
        comp = IndicatorEngine._adx_components(df, period)
        result = comp["minus_di"]
        logger.info("-DI(%d) = %.2f", period, result)
        return result
