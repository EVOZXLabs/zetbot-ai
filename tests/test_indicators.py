"""
Unit tests for IndicatorEngine.

Covers: flat market, uptrend, downtrend, random prices,
small dataset, large dataset, input validation, and a
performance benchmark.
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
#  Input validation
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
#  MarketData integration
# ---------------------------------------------------------------------------

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
        # EMA200 should be within ~20% of the current price
        assert 0.8 * latest <= ema_val <= 1.2 * latest


# ---------------------------------------------------------------------------
#  Performance benchmark
# ---------------------------------------------------------------------------

class TestPerformance:
    """EMA(200) on 1000 points must complete quickly."""

    def test_ema_1000_points_under_100ms(self) -> None:
        close = [float(i) for i in range(1000)]
        series = pd.Series(close)
        start = time.perf_counter()
        for _ in range(100):
            IndicatorEngine.ema(series, period=200)
        elapsed = (time.perf_counter() - start) / 100
        assert elapsed < 0.1, f"Average EMA took {elapsed*1000:.1f}ms (limit 100ms)"

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


# ---------------------------------------------------------------------------
#  Demonstration
# ---------------------------------------------------------------------------

def demo_ema200() -> None:
    """Fetch 250 hourly candles and display EMA200."""
    from bot.data import MarketData

    md = MarketData(exchange_name="binance")
    df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
    ema_val = md.ema200(df)
    latest = df["close"].iloc[-1]

    print(f"\n{'=' * 55}")
    print("EMA200 DEMONSTRATION")
    print(f"{'=' * 55}")
    print(f"Symbol      : BTC/USDT")
    print(f"Timeframe   : 1h")
    print(f"Candles     : {len(df)}")
    print(f"Latest close: ${latest:,.2f}")
    print(f"EMA200      : ${ema_val:,.2f}")
    print(f"Difference  : ${latest - ema_val:+,.2f}")
    gap_pct = (latest - ema_val) / ema_val * 100
    print(f"Gap         : {gap_pct:+.2f}%")
    signal = "BULLISH" if latest > ema_val else "BEARISH"
    print(f"Signal      : {signal}")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    demo_ema200()
