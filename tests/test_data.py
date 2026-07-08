"""Unit tests for MarketData module."""

import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from bot.data import NORMALIZED_COLUMNS, MarketData

SUPPORTED_EXCHANGES = ("binance", "bybit", "tokocrypto")


def test_invalid_exchange_raises_error() -> None:
    """MarketData should reject unsupported exchange names."""
    with pytest.raises(ValueError, match="Unsupported exchange"):
        MarketData(exchange_name="unknown_exchange")


def test_valid_exchange_initialises() -> None:
    """MarketData should initialise for all supported exchanges."""
    for name in SUPPORTED_EXCHANGES:
        md = MarketData(exchange_name=name)
        assert md.exchange_name == name
        assert md.exchange is not None


class TestLiveFetchOHLCV:
    """Live integration tests against Binance public API."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.md = MarketData(exchange_name="binance")

    def test_fetch_returns_dataframe(self) -> None:
        """fetch_ohlcv should return a pandas DataFrame."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        assert isinstance(df, pd.DataFrame)

    def test_fetch_expected_row_count(self) -> None:
        """DataFrame should contain the requested number of candles."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        assert len(df) == 100

    def test_fetch_expected_columns(self) -> None:
        """DataFrame should have exactly the standard 6 columns in order."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=10)
        assert list(df.columns) == NORMALIZED_COLUMNS

    def test_fetch_columns_have_correct_types(self) -> None:
        """Each column should have the expected pandas dtype."""
        timeframes = ["1h", "1d"]
        for tf in timeframes:
            df = self.md.fetch_ohlcv(
                symbol="BTC/USDT", timeframe=tf, limit=10,
            )
            assert isinstance(df, pd.DataFrame)
            assert pd.api.types.is_datetime64_any_dtype(df["timestamp"]), (
                f"timestamp must be datetime for {tf}"
            )
            for col in ["open", "high", "low", "close"]:
                assert pd.api.types.is_float_dtype(df[col]), (
                    f"{col} must be float for {tf}"
                )
            assert pd.api.types.is_float_dtype(df["volume"]), (
                f"volume must be float for {tf}"
            )

    def test_fetch_timestamps_are_utc(self) -> None:
        """Timestamps should be timezone-aware and UTC."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=10)
        for ts in df["timestamp"]:
            assert ts.tz is not None, "Timestamp must be timezone-aware"
            assert str(ts.tz) == "UTC", "Timestamp must be in UTC"

    def test_fetch_timestamps_are_unique(self) -> None:
        """All timestamps should be unique (no duplicates)."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)
        assert df["timestamp"].is_unique

    def test_fetch_prices_are_positive(self) -> None:
        """OHLC prices should be strictly positive."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=50)
        for col in ["open", "high", "low", "close"]:
            assert (df[col] > 0).all(), f"{col} must be > 0"

    def test_fetch_volume_is_non_negative(self) -> None:
        """Volume should be zero or positive."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=50)
        assert (df["volume"] >= 0).all(), "volume must be >= 0"

    def test_fetch_high_gte_low(self) -> None:
        """High should always be >= low for every candle."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)
        assert (df["high"] >= df["low"]).all()

    def test_fetch_close_within_range(self) -> None:
        """Close should be between low and high for every candle."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)
        assert (df["close"] >= df["low"]).all()
        assert (df["close"] <= df["high"]).all()

    def test_fetch_alternative_symbol(self) -> None:
        """Should work with ETH/USDT as well."""
        df = self.md.fetch_ohlcv(symbol="ETH/USDT", timeframe="1h", limit=50)
        assert len(df) == 50
        assert (df["close"] > 0).all()

    def test_fetch_bybit(self) -> None:
        """Should fetch from Bybit successfully."""
        md = MarketData(exchange_name="bybit")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=50)
        assert len(df) == 50
        assert list(df.columns) == NORMALIZED_COLUMNS

    def test_fetch_tokocrypto(self) -> None:
        """Should fetch from Tokocrypto successfully."""
        md = MarketData(exchange_name="tokocrypto")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=50)
        assert len(df) == 50
        assert list(df.columns) == NORMALIZED_COLUMNS

    def test_fetch_1m_timeframe(self) -> None:
        """Should fetch 1-minute candles."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1m", limit=60)
        assert len(df) == 60

    def test_fetch_1d_timeframe(self) -> None:
        """Should fetch daily candles."""
        df = self.md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1d", limit=30)
        assert len(df) == 30

    def test_fetch_different_limits(self) -> None:
        """Should handle various limit values correctly."""
        for limit in [10, 50, 500]:
            df = self.md.fetch_ohlcv(
                symbol="BTC/USDT", timeframe="1h", limit=limit,
            )
            assert len(df) == limit, f"Expected {limit}, got {len(df)}"


def demo_fetch_btc_1h_200() -> None:
    """Demonstrate fetching 200 hourly BTC/USDT candles from Binance."""
    md = MarketData(exchange_name="binance")
    df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=200)

    print(f"\n{'=' * 60}")
    print(f"BTC/USDT 1h 200 candles from Binance")
    print(f"{'=' * 60}")
    print(f"First 5 rows:")
    print(df.head(5).to_string(index=False))
    print(f"\nLast 5 rows:")
    print(df.tail(5).to_string(index=False))
    print(f"\nShape: {df.shape}")
    print(f"Date range: {df['timestamp'].min()} → {df['timestamp'].max()}")
    print(f"Price range: ${df['low'].min():.2f} → ${df['high'].max():.2f}")
    print(f"Latest close: ${df['close'].iloc[-1]:.2f}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    demo_fetch_btc_1h_200()
