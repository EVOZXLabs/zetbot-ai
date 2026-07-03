"""
Unit tests for MarketStateDetector.

Covers: input validation, trending markets, sideways markets,
volatility scenarios, edge cases, MarketData integration.
"""

import math

import pandas as pd
import pytest

from bot.state import MarketStateDetector, SIDEWAYS, TRENDING


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _trending_df(n: int = 80) -> pd.DataFrame:
    """Strong uptrend → ADX should be well above 25."""
    highs = [100.0 + i * 2.0 for i in range(n)]
    lows  = [90.0 + i * 1.8 for i in range(n)]
    close = [95.0 + i * 2.0 for i in range(n)]
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _sideways_df(n: int = 80) -> pd.DataFrame:
    """Tight range, no direction → ADX near 0."""
    base = 100.0
    return pd.DataFrame({
        "high": [base + 1.0] * n,
        "low":  [base - 1.0] * n,
        "close":[base] * n,
    })


def _compressed_volatility_df(n: int = 80) -> pd.DataFrame:
    """Wide then narrow range → volatility compression."""
    highs = []
    lows = []
    close = []
    for i in range(n):
        if i < 40:
            highs.append(120.0 + i * 0.5)
            lows.append(80.0 - i * 0.5)
            close.append(100.0)
        else:
            highs.append(101.0)
            lows.append(99.0)
            close.append(100.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


# ---------------------------------------------------------------------------
#  Input validation
# ---------------------------------------------------------------------------

class TestStateValidation:

    def test_empty_df_raises(self) -> None:
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="empty"):
            detector.detect(pd.DataFrame())

    def test_missing_high_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "low": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="high"):
            detector.detect(df)

    def test_missing_low_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "high": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="low"):
            detector.detect(df)

    def test_missing_close_raises(self) -> None:
        df = pd.DataFrame({"high": [1.0], "low": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="close"):
            detector.detect(df)


# ---------------------------------------------------------------------------
#  Trending market detection
# ---------------------------------------------------------------------------

class TestTrendingDetection:

    def test_strong_uptrend_returns_trending(self) -> None:
        df = _trending_df(80)
        detector = MarketStateDetector(adx_threshold=25)
        result = detector.detect(df)
        assert result == TRENDING, f"Expected TRENDING, got {result}"

    def test_strong_downtrend_returns_trending(self) -> None:
        n = 80
        highs = [200.0 - i * 2.0 for i in range(n)]
        lows  = [190.0 - i * 2.0 for i in range(n)]
        close = [195.0 - i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=25)
        result = detector.detect(df)
        assert result == TRENDING, f"Expected TRENDING, got {result}"

    def test_low_adx_threshold_catches_weak_trend(self) -> None:
        """With a low threshold, even weak trends are TRENDING."""
        n = 80
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows  = [90.0 + i * 0.4 for i in range(n)]
        close = [95.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=10)
        result = detector.detect(df)
        assert result == TRENDING

    def test_high_adx_threshold_rejects_random_walk(self) -> None:
        """With a high threshold, random walk is SIDEWAYS."""
        import random
        random.seed(42)
        n = 80
        base = 100.0
        highs = []
        lows = []
        close = [base]
        for i in range(1, n):
            prev = close[-1]
            change = random.uniform(-1.0, 1.0)
            cur = prev + change
            close.append(cur)
            highs.append(max(prev, cur) + random.uniform(0, 0.5))
            lows.append(min(prev, cur) - random.uniform(0, 0.5))
        highs.insert(0, base + 1.0)
        lows.insert(0, base - 1.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=50)
        result = detector.detect(df)
        assert result == SIDEWAYS


# ---------------------------------------------------------------------------
#  Sideways market detection
# ---------------------------------------------------------------------------

class TestSidewaysDetection:

    def test_flat_market_returns_sideways(self) -> None:
        df = _sideways_df(80)
        detector = MarketStateDetector()
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_compressed_volatility_returns_sideways(self) -> None:
        df = _compressed_volatility_df(80)
        detector = MarketStateDetector(atr_multiplier=0.5)
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_price_compression_returns_sideways(self) -> None:
        """Narrow recent range relative to long range."""
        n = 80
        highs = []
        lows = []
        close = []
        for i in range(n):
            if i < 60:
                highs.append(120.0)
                lows.append(80.0)
                close.append(100.0)
            else:
                highs.append(101.0)
                lows.append(99.0)
                close.append(100.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(
            adx_threshold=50,
            compression_lookback=20,
            compression_ratio=0.3,
        )
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_small_dataset_falls_back_to_sideways(self) -> None:
        """Very small dataset (but valid OHLC) → SIDEWAYS."""
        n = 30
        base = 100.0
        df = pd.DataFrame({
            "high": [base + 0.5] * n,
            "low":  [base - 0.5] * n,
            "close":[base] * n,
        })
        detector = MarketStateDetector()
        result = detector.detect(df)
        assert result == SIDEWAYS


# ---------------------------------------------------------------------------
#  MarketData integration
# ---------------------------------------------------------------------------

class TestMarketDataStateIntegration:
    """Verify market_state() works after fetch_ohlcv."""

    def test_market_state_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.market_state(df)
        assert result in (TRENDING, SIDEWAYS)

    def test_market_state_is_string(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.market_state(df)
        assert isinstance(result, str)
        assert result in (TRENDING, SIDEWAYS)
