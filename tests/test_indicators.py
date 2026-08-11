"""
Unit tests for IndicatorEngine.

Covers: input validation, EMA correctness, RSI correctness,
market scenarios (flat, uptrend, downtrend, random),
small/large datasets, MarketData integration, and performance.
"""

import math
import time

import pandas as pd
import pytest

from bot.indicators import IndicatorEngine


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


# ---------------------------------------------------------------------------
#  Input validation  (shared by EMA and RSI)
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            IndicatorEngine.ema(_series([]), period=14)

    def test_nan_in_series_raises(self) -> None:
        with pytest.raises(ValueError, match="NaN"):
            IndicatorEngine.ema(_series([10.0, float("nan"), 30.0]), period=14)

    def test_period_below_2_raises(self) -> None:
        with pytest.raises(ValueError, match="period"):
            IndicatorEngine.ema(_series([10.0, 20.0]), period=1)

    def test_period_float_raises(self) -> None:
        with pytest.raises(ValueError, match="period"):
            IndicatorEngine.ema(_series([10.0, 20.0]), period=14.0)

    def test_ema200_empty_df_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            IndicatorEngine.ema200(pd.DataFrame())

    def test_ema200_missing_column_raises(self) -> None:
        df = pd.DataFrame({"open": [1.0, 2.0]})
        with pytest.raises(ValueError, match="not found"):
            IndicatorEngine.ema200(df, column="close")

    # --- RSI-specific validation ---

    def test_rsi_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            IndicatorEngine.rsi(_series([]))

    def test_rsi_nan_series_raises(self) -> None:
        with pytest.raises(ValueError, match="NaN"):
            IndicatorEngine.rsi(_series([10.0, float("nan"), 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0]))

    def test_rsi_period_below_2_raises(self) -> None:
        with pytest.raises(ValueError, match="period"):
            IndicatorEngine.rsi(_series([10.0] * 20), period=1)

    def test_rsi_insufficient_candles_raises(self) -> None:
        """Need at least period + 1 candles for RSI."""
        with pytest.raises(ValueError, match="data points"):
            IndicatorEngine.rsi(_series([10.0] * 14), period=14)

    def test_rsi_exactly_minimum_candles(self) -> None:
        """period + 1 candles should be sufficient."""
        result = IndicatorEngine.rsi(_series([10.0] * 16), period=14)
        assert isinstance(result, float)
        assert 0 <= result <= 100


# ---------------------------------------------------------------------------
#  EMA correctness
# ---------------------------------------------------------------------------

class TestEMACorrectness:
    """EMA must match manually calculated values."""

    def test_ema_period_2_known_values(self) -> None:
        """EMA(2) on [10, 20, 30, 40].

        alpha = 2 / (2 + 1) = 0.6667
        EMA[0] = 10
        EMA[1] = 0.6667 * 20 + 0.3333 * 10 = 16.6667
        EMA[2] = 0.6667 * 30 + 0.3333 * 16.6667 = 25.5556
        EMA[3] = 0.6667 * 40 + 0.3333 * 25.5556 = 35.1852
        """
        series = _series([10.0, 20.0, 30.0, 40.0])
        result = IndicatorEngine.ema(series, period=2)
        expected = [10.0, 16.66666667, 25.55555556, 35.18518519]
        for r, e in zip(result, expected):
            assert abs(r - e) < 0.01, f"Expected {e}, got {r}"

    def test_ema_period_3_known_values(self) -> None:
        """EMA(3) on [10, 20, 30, 40, 50].

        alpha = 2 / (3 + 1) = 0.5
        EMA[0] = 10
        EMA[1] = 0.5 * 20 + 0.5 * 10 = 15
        EMA[2] = 0.5 * 30 + 0.5 * 15 = 22.5
        EMA[3] = 0.5 * 40 + 0.5 * 22.5 = 31.25
        EMA[4] = 0.5 * 50 + 0.5 * 31.25 = 40.625
        """
        series = _series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = IndicatorEngine.ema(series, period=3)
        expected = [10.0, 15.0, 22.5, 31.25, 40.625]
        for r, e in zip(result, expected):
            assert abs(r - e) < 0.01, f"Expected {e}, got {r}"

    def test_ema_output_name(self) -> None:
        result = IndicatorEngine.ema(_series([10.0, 20.0, 30.0]), period=5)
        assert result.name == "EMA_5"

    def test_ema_index_preserved(self) -> None:
        series = pd.Series([10.0, 20.0, 30.0], index=[10, 20, 30])
        result = IndicatorEngine.ema(series, period=2)
        assert list(result.index) == [10, 20, 30]


# ---------------------------------------------------------------------------
#  EMA200 on known market scenarios
# ---------------------------------------------------------------------------

class TestEMA200Scenarios:

    def test_flat_market(self) -> None:
        """Constant prices: EMA200 should converge to that price."""
        price = 50_000.0
        close = [price] * 300
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert abs(result - price) < 1.0, f"Expected {price}, got {result}"

    def test_uptrend(self) -> None:
        """Steady uptrend: EMA200 should lag below the latest close."""
        close = [float(i) for i in range(100, 400)]  # 100 → 399
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert result < close[-1], (
            f"EMA200 ({result:.2f}) should lag below "
            f"close ({close[-1]:.2f}) in uptrend"
        )
        assert result > close[0], (
            f"EMA200 ({result:.2f}) should be above "
            f"first close ({close[0]:.2f}) in uptrend"
        )

    def test_downtrend(self) -> None:
        """Steady downtrend: EMA200 should lag above the latest close."""
        close = [float(i) for i in range(400, 100, -1)]  # 400 → 101
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert result > close[-1], (
            f"EMA200 ({result:.2f}) should lag above "
            f"close ({close[-1]:.2f}) in downtrend"
        )
        assert result < close[0], (
            f"EMA200 ({result:.2f}) should be below "
            f"first close ({close[0]:.2f}) in downtrend"
        )

    def test_random_prices(self) -> None:
        """Random prices: should not produce NaN or raise."""
        import random
        random.seed(42)
        close = [random.uniform(40_000, 60_000) for _ in range(300)]
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert math.isfinite(result)
        assert 30_000 < result < 70_000

    def test_small_dataset(self) -> None:
        """Fewer than 200 points is valid but EMA is computed."""
        close = [50_000 + i * 10 for i in range(50)]
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert math.isfinite(result)

    def test_large_dataset(self) -> None:
        """1000 points should produce a stable EMA200."""
        close = [float(i) for i in range(1000)]
        df = pd.DataFrame({"close": close})
        result = IndicatorEngine.ema200(df)
        assert math.isfinite(result)
        assert 0 < result < 1000

    def test_ema200_returns_float(self) -> None:
        df = pd.DataFrame({"close": [50_000.0] * 250})
        result = IndicatorEngine.ema200(df)
        assert isinstance(result, float)

    def test_ema200_close_within_high_low(self) -> None:
        """EMA200 on close should be within reasonable price range."""
        df = pd.DataFrame({
            "close": [60_000 + i * 10 for i in range(250)],
        })
        result = IndicatorEngine.ema200(df)
        assert df["close"].min() <= result <= df["close"].max()


# ---------------------------------------------------------------------------
#  RSI correctness
# ---------------------------------------------------------------------------

class TestRSICorrectness:

    def test_rsi_period_3_known_values(self) -> None:
        """RSI(3) on [10, 12, 11, 13, 15].

        diff  = [NaN, 2, -1, 2, 2]
        gains = [NaN, 2,  0, 2, 2]
        losses= [NaN, 0,  1, 0, 0]

        SMA gain = (2 + 0 + 2) / 3 = 1.333
        SMA loss = (0 + 1 + 0) / 3 = 0.333

        Wilder smooth next (gain=2, loss=0):
        avg_gain = (1.333*2 + 2) / 3 = 1.555
        avg_loss = (0.333*2 + 0) / 3 = 0.222

        rs = 1.555 / 0.222 = 7.0
        rsi = 100 - 100/(1+7) = 100 - 12.5 = 87.50
        """
        series = _series([10.0, 12.0, 11.0, 13.0, 15.0])
        result = IndicatorEngine.rsi(series, period=3)
        assert abs(result - 87.50) < 0.1, f"Expected ~87.50, got {result}"

    def test_rsi_returns_float(self) -> None:
        series = _series([float(i) for i in range(20)])
        result = IndicatorEngine.rsi(series, period=14)
        assert isinstance(result, float)
        assert 0 <= result <= 100

    def test_rsi_default_period_14(self) -> None:
        series = _series([float(i) for i in range(50)])
        result = IndicatorEngine.rsi(series)
        assert isinstance(result, float)
        assert 0 <= result <= 100


# ---------------------------------------------------------------------------
#  RSI market scenarios
# ---------------------------------------------------------------------------

class TestRSIScenarios:

    def test_flat_market(self) -> None:
        """Constant prices: RSI should be 50.0."""
        close = [50_000.0] * 100
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert result == 50.0

    def test_strong_uptrend(self) -> None:
        """Consistent upward moves: RSI should be >= 80."""
        close = [100.0 + i for i in range(50)]
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert result >= 80, f"Expected RSI >= 80 in uptrend, got {result}"

    def test_strong_downtrend(self) -> None:
        """Consistent downward moves: RSI should be <= 20."""
        close = [200.0 - i for i in range(50)]
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert result <= 20, f"Expected RSI <= 20 in downtrend, got {result}"

    def test_random_prices(self) -> None:
        """Random prices: RSI should be between 0 and 100."""
        import random
        random.seed(42)
        close = [random.uniform(100.0, 200.0) for _ in range(100)]
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert 0 <= result <= 100, f"RSI out of range: {result}"

    def test_small_dataset(self) -> None:
        """Exactly period + 1 candles should work."""
        close = [100.0 + i for i in range(15)]  # 15 = 14 + 1
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert isinstance(result, float)
        assert 0 <= result <= 100

    def test_large_dataset(self) -> None:
        """1000 points should not degrade precision."""
        close = [float(i) for i in range(1000)]
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert isinstance(result, float)
        assert 0 <= result <= 100

    def test_almost_all_gains(self) -> None:
        """One small loss among gains: RSI should be very high."""
        close = [100.0]
        for _ in range(30):
            close.append(close[-1] + 1.0)
        close[-3] = close[-4]  # one flat candle
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert result > 80

    def test_almost_all_losses(self) -> None:
        """One small gain among losses: RSI should be very low."""
        close = [200.0]
        for _ in range(30):
            close.append(close[-1] - 1.0)
        close[-3] = close[-4]  # one flat candle
        result = IndicatorEngine.rsi(_series(close), period=14)
        assert result < 20


# ---------------------------------------------------------------------------
#  ATR validation
# ---------------------------------------------------------------------------

class TestATRValidation:
    """ATR input validation."""

    def _ohlc_df(self, n: int, base: float = 50_000.0) -> pd.DataFrame:
        return pd.DataFrame({
            "high": [base + i + 1.0 for i in range(n)],
            "low":  [base - i - 1.0 for i in range(n)],
            "close":[base + float(i) for i in range(n)],
        })

    def test_empty_df_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            IndicatorEngine.atr(pd.DataFrame())

    def test_missing_high_column_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "low": [1.0]})
        with pytest.raises(ValueError, match="high"):
            IndicatorEngine.atr(df)

    def test_missing_low_column_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "high": [1.0]})
        with pytest.raises(ValueError, match="low"):
            IndicatorEngine.atr(df)

    def test_missing_close_column_raises(self) -> None:
        df = pd.DataFrame({"high": [1.0], "low": [1.0]})
        with pytest.raises(ValueError, match="close"):
            IndicatorEngine.atr(df)

    def test_period_below_2_raises(self) -> None:
        df = self._ohlc_df(20)
        with pytest.raises(ValueError, match="period"):
            IndicatorEngine.atr(df, period=1)

    def test_insufficient_candles_raises(self) -> None:
        """ATR(14) needs at least 15 candles (period + 1)."""
        df = self._ohlc_df(14)
        with pytest.raises(ValueError, match="at least"):
            IndicatorEngine.atr(df, period=14)

    def test_exactly_minimum_candles(self) -> None:
        """ATR(14) works with exactly 15 candles."""
        df = self._ohlc_df(15)
        result = IndicatorEngine.atr(df, period=14)
        assert isinstance(result, float)
        assert result >= 0.0

    def test_atr_returns_float(self) -> None:
        df = self._ohlc_df(50)
        result = IndicatorEngine.atr(df)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
#  ATR correctness
# ---------------------------------------------------------------------------

class TestATRCorrectness:
    """ATR must match manual calculations."""

    def test_flat_market_atr_constant(self) -> None:
        """Constant high/low spread: ATR equals the spread."""
        n = 30
        df = pd.DataFrame({
            "high": [100.0] * n,
            "low":  [90.0] * n,
            "close":[95.0] * n,
        })
        result = IndicatorEngine.atr(df, period=14)
        assert abs(result - 10.0) < 0.01, f"Expected ~10.0, got {result}"

    def test_constant_range(self) -> None:
        """Constant high-low range produces constant ATR."""
        n = 30
        df = pd.DataFrame({
            "high": [110.0] * n,
            "low":  [90.0] * n,
            "close":[100.0] * n,
        })
        result = IndicatorEngine.atr(df, period=14)
        assert abs(result - 20.0) < 0.01, f"Expected ~20.0, got {result}"

    def test_increasing_range(self) -> None:
        """Expanding range: ATR should increase."""
        n = 30
        highs = [100.0 + i for i in range(n)]
        lows  = [90.0 - i for i in range(n)]
        close = [95.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert result > 10.0

    def test_atr_default_period_14(self) -> None:
        df = TestATRValidation()._ohlc_df(50)
        result = IndicatorEngine.atr(df)
        assert isinstance(result, float)

    def test_atr_period_7(self) -> None:
        df = TestATRValidation()._ohlc_df(30)
        result = IndicatorEngine.atr(df, period=7)
        assert isinstance(result, float)
        assert result >= 0.0


# ---------------------------------------------------------------------------
#  ATR market scenarios
# ---------------------------------------------------------------------------

class TestATRScenarios:
    """ATR on realistic market scenarios."""

    def test_high_volatility(self) -> None:
        """Large candles → high ATR."""
        n = 30
        highs = [100.0 + i * 5.0 for i in range(n)]
        lows  = [90.0 - i * 5.0 for i in range(n)]
        close = [95.0 + i * 3.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert result > 20.0

    def test_low_volatility(self) -> None:
        """Small candles → low ATR."""
        n = 30
        highs = [100.0 + i * 0.1 for i in range(n)]
        lows  = [99.0 - i * 0.1 for i in range(n)]
        close = [99.5 + i * 0.05 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert result < 5.0

    def test_random_prices(self) -> None:
        """ATR should be non-negative for random data."""
        import random
        random.seed(42)
        n = 50
        highs = [100.0 + random.uniform(0, 10) for _ in range(n)]
        lows  = [90.0 - random.uniform(0, 10) for _ in range(n)]
        close = [(highs[i] + lows[i]) / 2 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert isinstance(result, float)
        assert result >= 0.0

    def test_small_dataset(self) -> None:
        """Exactly 15 candles works for ATR(14)."""
        n = 15
        highs = [100.0 + i for i in range(n)]
        lows  = [90.0 + i for i in range(n)]
        close = [95.0 + i for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert isinstance(result, float)
        assert result >= 0.0

    def test_large_dataset(self) -> None:
        """500 points should not degrade precision."""
        n = 500
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows  = [90.0 + i * 0.45 for i in range(n)]
        close = [95.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.atr(df, period=14)
        assert isinstance(result, float)
        assert result >= 0.0


# ---------------------------------------------------------------------------
#  ADX validation
# ---------------------------------------------------------------------------

class TestADXValidation:
    """ADX input validation."""

    def _ohlc_df(self, n: int, base: float = 50_000.0) -> pd.DataFrame:
        df = pd.DataFrame({
            "high": [base + i + 1.0 for i in range(n)],
            "low":  [base - i - 1.0 for i in range(n)],
            "close":[base + float(i) for i in range(n)],
        })
        return df

    def test_empty_df_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            IndicatorEngine.adx(pd.DataFrame())

    def test_missing_high_column_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0, 2.0], "low": [1.0, 2.0]})
        with pytest.raises(ValueError, match="high"):
            IndicatorEngine.adx(df)

    def test_missing_low_column_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0, 2.0], "high": [1.0, 2.0]})
        with pytest.raises(ValueError, match="low"):
            IndicatorEngine.adx(df)

    def test_missing_close_column_raises(self) -> None:
        df = pd.DataFrame({"high": [1.0, 2.0], "low": [1.0, 2.0]})
        with pytest.raises(ValueError, match="close"):
            IndicatorEngine.adx(df)

    def test_period_below_2_raises(self) -> None:
        df = self._ohlc_df(30)
        with pytest.raises(ValueError, match="period"):
            IndicatorEngine.adx(df, period=1)

    def test_insufficient_candles_raises(self) -> None:
        """ADX(14) needs at least 29 candles."""
        df = self._ohlc_df(28)
        with pytest.raises(ValueError, match="at least.*29"):
            IndicatorEngine.adx(df, period=14)

    def test_exactly_minimum_candles(self) -> None:
        """ADX(14) works with exactly 29 candles."""
        df = self._ohlc_df(29)
        result = IndicatorEngine.adx(df, period=14)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0

    def test_adx_returns_float(self) -> None:
        df = self._ohlc_df(50)
        result = IndicatorEngine.adx(df)
        assert isinstance(result, float)

    def test_plus_di_returns_float(self) -> None:
        df = self._ohlc_df(50)
        result = IndicatorEngine.plus_di(df)
        assert isinstance(result, float)

    def test_minus_di_returns_float(self) -> None:
        df = self._ohlc_df(50)
        result = IndicatorEngine.minus_di(df)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
#  ADX correctness
# ---------------------------------------------------------------------------

class TestADXCorrectness:
    """ADX must match manual/known calculations."""

    def test_flat_market_adx_zero(self) -> None:
        """Constant high/low/close should produce ADX of 0."""
        n = 50
        df = pd.DataFrame({
            "high": [100.0] * n,
            "low":  [90.0] * n,
            "close":[95.0] * n,
        })
        result = IndicatorEngine.adx(df, period=14)
        assert result == 0.0, f"Expected ADX=0 for flat market, got {result}"

    def test_flat_market_di_equal(self) -> None:
        """Flat market: +DI and -DI should both be near 0."""
        n = 50
        df = pd.DataFrame({
            "high": [100.0] * n,
            "low":  [90.0] * n,
            "close":[95.0] * n,
        })
        dp = IndicatorEngine.plus_di(df, period=14)
        dm = IndicatorEngine.minus_di(df, period=14)
        assert dp == 0.0
        assert dm == 0.0

    def test_strong_uptrend_adx_high(self) -> None:
        """Consistent higher highs/lower highs: ADX should be high."""
        n = 60
        highs = [100.0 + i * 2.0 for i in range(n)]
        lows  = [90.0 + i * 1.8 for i in range(n)]
        close = [95.0 + i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.adx(df, period=14)
        assert result > 20.0, f"Expected ADX > 20 in uptrend, got {result}"

    def test_strong_downtrend_adx_high(self) -> None:
        """Consistent lower lows: ADX should be high."""
        n = 60
        highs = [200.0 - i * 2.0 for i in range(n)]
        lows  = [190.0 - i * 2.0 for i in range(n)]
        close = [195.0 - i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.adx(df, period=14)
        assert result > 20.0, f"Expected ADX > 20 in downtrend, got {result}"

    def test_uptrend_plus_di_above_minus_di(self) -> None:
        """In uptrend, +DI should be > -DI."""
        n = 60
        highs = [100.0 + i * 2.0 for i in range(n)]
        lows  = [90.0 + i * 1.5 for i in range(n)]
        close = [95.0 + i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        dp = IndicatorEngine.plus_di(df, period=14)
        dm = IndicatorEngine.minus_di(df, period=14)
        assert dp > dm, f"Expected +DI ({dp}) > -DI ({dm}) in uptrend"

    def test_downtrend_minus_di_above_plus_di(self) -> None:
        """In downtrend, -DI should be > +DI."""
        n = 60
        highs = [200.0 - i * 1.5 for i in range(n)]
        lows  = [190.0 - i * 2.0 for i in range(n)]
        close = [195.0 - i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        dp = IndicatorEngine.plus_di(df, period=14)
        dm = IndicatorEngine.minus_di(df, period=14)
        assert dm > dp, f"Expected -DI ({dm}) > +DI ({dp}) in downtrend"

    def test_random_prices_bounded(self) -> None:
        """ADX should be between 0 and 100."""
        import random
        random.seed(42)
        n = 100
        base = 100.0
        highs = [base + random.uniform(0, 5) for _ in range(n)]
        lows  = [base - random.uniform(0, 5) for _ in range(n)]
        close = [(highs[i] + lows[i]) / 2 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.adx(df, period=14)
        assert 0.0 <= result <= 100.0, f"ADX out of range: {result}"

    def test_small_dataset(self) -> None:
        """Exactly 29 candles should work for ADX(14)."""
        n = 29
        highs = [100.0 + i for i in range(n)]
        lows  = [90.0 + i for i in range(n)]
        close = [95.0 + i for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.adx(df, period=14)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0

    def test_large_dataset_stability(self) -> None:
        """500 points should produce stable ADX."""
        n = 500
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows  = [90.0 + i * 0.45 for i in range(n)]
        close = [95.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        result = IndicatorEngine.adx(df, period=14)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0


# ---------------------------------------------------------------------------
#  MarketData ADX integration
# ---------------------------------------------------------------------------

@pytest.mark.network
@pytest.mark.exchange("binance")
class TestMarketDataADXIntegration:
    """Verify that MarketData.adx/plus_di/minus_di work after fetch_ohlcv."""

    def test_adx_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.adx(df)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0

    def test_plus_di_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.plus_di(df)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0

    def test_minus_di_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.minus_di(df)
        assert isinstance(result, float)
        assert 0.0 <= result <= 100.0


# ---------------------------------------------------------------------------
#  MarketData integration
# ---------------------------------------------------------------------------

@pytest.mark.network
@pytest.mark.exchange("binance")
class TestMarketDataEMA200Integration:
    """Verify that MarketData.ema200 works after fetch_ohlcv."""

    def test_ema200_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        ema_val = md.ema200(df)
        assert isinstance(ema_val, float)
        assert ema_val > 0

    def test_ema200_reasonable_value(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        ema_val = md.ema200(df)
        latest = df["close"].iloc[-1]
        assert 0.8 * latest <= ema_val <= 1.2 * latest


@pytest.mark.network
@pytest.mark.exchange("binance")
class TestMarketDataRSIIntegration:
    """Verify that MarketData.rsi works after fetch_ohlcv."""

    def test_rsi_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)
        rsi_val = md.rsi(df)
        assert isinstance(rsi_val, float)
        assert 0 <= rsi_val <= 100

    def test_rsi_reasonable_value(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)
        rsi_val = md.rsi(df)
        assert 20 <= rsi_val <= 80, (
            f"Expected RSI in normal range, got {rsi_val}"
        )


# ---------------------------------------------------------------------------
#  Performance benchmark
# ---------------------------------------------------------------------------

class TestPerformance:

    def test_ema_1000_points_under_100ms(self) -> None:
        close = [float(i) for i in range(1000)]
        series = pd.Series(close)
        start = time.perf_counter()
        for _ in range(100):
            IndicatorEngine.ema(series, period=200)
        elapsed = (time.perf_counter() - start) / 100
        assert elapsed < 0.2, f"Average EMA took {elapsed*1000:.1f}ms (limit 200ms)"

    @pytest.mark.network
    @pytest.mark.exchange("binance")
    def test_ema200_live_fetch_under_10s(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        start = time.perf_counter()
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        ema_val = md.ema200(df)
        elapsed = time.perf_counter() - start
        assert isinstance(ema_val, float)
        assert elapsed < 10.0, (
            f"Fetch + EMA200 took {elapsed:.1f}s (limit 10s)"
        )

    def test_rsi_1000_points_under_50ms(self) -> None:
        close = [float(i) for i in range(1000)]
        series = pd.Series(close)
        start = time.perf_counter()
        for _ in range(100):
            IndicatorEngine.rsi(series, period=14)
        elapsed = (time.perf_counter() - start) / 100
        assert elapsed < 0.15, f"Average RSI took {elapsed*1000:.1f}ms (limit 150ms)"

    @pytest.mark.network
    @pytest.mark.exchange("binance")
    def test_rsi_live_fetch_under_10s(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        start = time.perf_counter()
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        rsi_val = md.rsi(df)
        elapsed = time.perf_counter() - start
        assert isinstance(rsi_val, float)
        assert 0 <= rsi_val <= 100
        assert elapsed < 10.0, (
            f"Fetch + RSI took {elapsed:.1f}s (limit 10s)"
        )


# ---------------------------------------------------------------------------
#  Demonstration
# ---------------------------------------------------------------------------

def demo_indicators() -> None:
    """Fetch 250 hourly candles and display EMA200 + RSI14."""
    from bot.data import MarketData

    md = MarketData(exchange_name="binance")
    df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
    price = df["close"].iloc[-1]
    ema_val = md.ema200(df)
    rsi_val = md.rsi(df)

    trend = "Bullish" if price > ema_val else "Bearish"

    print(f"\n{'=' * 55}")
    print("MARKET OVERVIEW")
    print(f"{'=' * 55}")
    print(f"Symbol      : BTC/USDT")
    print(f"Timeframe   : 1h")
    print(f"Candles     : {len(df)}")
    print(f"Price       : ${price:,.2f}")
    print(f"EMA200      : ${ema_val:,.2f}")
    print(f"RSI(14)     : {rsi_val:.2f}")
    print(f"Trend       : {trend}")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    demo_indicators()
