"""
Market Data Module

ZetBot AI
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import ccxt
import pandas as pd

from bot.indicators import IndicatorEngine

logger = logging.getLogger("ZetBot")

EXCHANGE_MAP: dict[str, type[ccxt.Exchange]] = {
    "binance": ccxt.binance,
    "bybit": ccxt.bybit,
    "tokocrypto": ccxt.binance,
}

NORMALIZED_COLUMNS: list[str] = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


class MarketData:
    """Fetch and validate OHLCV market data from supported exchanges.

    Supports Binance, Bybit, and Tokocrypto spot markets.
    Data is returned as a pandas DataFrame with normalized column names.
    """

    def __init__(
        self,
        exchange_name: str = "binance",
        api_key: str = "",
        secret: str = "",
    ) -> None:
        """Initialize the market data fetcher.

        Args:
            exchange_name: Exchange identifier. One of 'binance', 'bybit',
                'tokocrypto'.
            api_key: API key for authenticated endpoints.
            secret: API secret for authenticated endpoints.

        Raises:
            ValueError: If the exchange is not supported.
        """
        exchange_name = exchange_name.lower()
        exchange_class = EXCHANGE_MAP.get(exchange_name)
        if exchange_class is None:
            msg = (
                f"Unsupported exchange: '{exchange_name}'. "
                f"Supported: {', '.join(EXCHANGE_MAP)}"
            )
            raise ValueError(msg)

        self.exchange: ccxt.Exchange = exchange_class({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
        })
        self.exchange_name: str = exchange_name

        logger.info("MarketData initialized for %s", self.exchange_name)

    def fetch_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles and return a normalized DataFrame.

        Validates the response for empty data, missing candles, and
        invalid price values before returning.

        Args:
            symbol: Trading pair symbol (e.g. 'BTC/USDT').
            timeframe: Candle timeframe (e.g. '1m', '5m', '15m', '30m',
                '1h', '4h', '1d').
            limit: Number of candles to fetch (max depends on exchange).

        Returns:
            DataFrame with columns:
                timestamp (datetime, UTC), open, high, low, close, volume.

        Raises:
            ValueError: If the exchange returns no candles or the response
                is malformed.
            ccxt.NetworkError: If the exchange is unreachable.
            ccxt.ExchangeError: If the exchange returns an error.
        """
        logger.info(
            "Fetching %d %s candles for %s from %s",
            limit, timeframe, symbol, self.exchange_name,
        )

        raw: list[list[Any]] = self.exchange.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
        )

        if not raw:
            msg = (
                f"Empty response from {self.exchange_name} for "
                f"{symbol} {timeframe}"
            )
            logger.error(msg)
            raise ValueError(msg)

        expected = limit
        actual = len(raw)
        if actual < expected:
            logger.warning(
                "Requested %d candles, received %d from %s",
                expected, actual, self.exchange_name,
            )

        df = pd.DataFrame(raw, columns=NORMALIZED_COLUMNS)

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            unit="ms",
            utc=True,
        )

        self._validate_dataframe(df, symbol, timeframe)

        logger.info(
            "Fetched %d %s candles for %s",
            len(df), timeframe, symbol,
        )
        return df

    def _validate_dataframe(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
    ) -> None:
        """Validate dataframe content for data quality issues.

        Checks for duplicate timestamps, missing candles (gaps), and
        invalid prices.

        Args:
            df: DataFrame to validate.
            symbol: Trading pair (for error messages).
            timeframe: Candle timeframe (for gap detection).

        Raises:
            ValueError: If data validation fails.
        """
        duplicates = df["timestamp"].duplicated()
        if duplicates.any():
            count = int(duplicates.sum())
            logger.warning(
                "Found %d duplicate timestamps in %s %s data",
                count, symbol, timeframe,
            )

        nan_prices = df[["open", "high", "low", "close"]].isnull().any(axis=1)
        if nan_prices.any():
            count = int(nan_prices.sum())
            msg = (
                f"Found {count} candle(s) with NaN prices in "
                f"{symbol} {timeframe}"
            )
            logger.error(msg)
            raise ValueError(msg)

        zero_prices = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        if zero_prices.any():
            count = int(zero_prices.sum())
            msg = (
                f"Found {count} candle(s) with non-positive prices in "
                f"{symbol} {timeframe}"
            )
            logger.error(msg)
            raise ValueError(msg)

        negative_volume = (df["volume"] < 0).any()
        if negative_volume:
            count = int((df["volume"] < 0).sum())
            msg = (
                f"Found {count} candle(s) with negative volume in "
                f"{symbol} {timeframe}"
            )
            logger.error(msg)
            raise ValueError(msg)

    def ema200(self, df: pd.DataFrame, column: str = "close") -> float:
        """Calculate the latest EMA200 value on fetched data.

        Convenience method that delegates to ``IndicatorEngine.ema200``.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            column: Price column to use (default ``"close"``).

        Returns:
            Latest EMA200 value as a float.
        """
        return IndicatorEngine.ema200(df, column=column)

    def rsi(
        self,
        df: pd.DataFrame,
        column: str = "close",
        period: int = 14,
    ) -> float:
        """Calculate the latest RSI value on fetched data.

        Convenience method that delegates to ``IndicatorEngine.rsi``.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            column: Price column to use (default ``"close"``).
            period: RSI period (default 14).

        Returns:
            Latest RSI value as a float.
        """
        return IndicatorEngine.rsi(df[column], period=period)

    def adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest ADX value on fetched data.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            period: ADX period (default 14).

        Returns:
            Latest ADX value as a float.
        """
        return IndicatorEngine.adx(df, period=period)

    def plus_di(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest +DI value on fetched data.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            period: DI period (default 14).

        Returns:
            Latest +DI value as a float.
        """
        return IndicatorEngine.plus_di(df, period=period)

    def minus_di(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest -DI value on fetched data.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            period: DI period (default 14).

        Returns:
            Latest -DI value as a float.
        """
        return IndicatorEngine.minus_di(df, period=period)
