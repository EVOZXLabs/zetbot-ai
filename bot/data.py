"""
Market Data Module

ZetBot AI
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from bot.config import CONFIG
from bot.indicators import IndicatorEngine
from bot.state import MarketStateDetector

logger = logging.getLogger("ZetBot")


def _get_exchange_map():
    import ccxt
    return {
        "binance": ccxt.binance,
        "bybit": ccxt.bybit,
        "tokocrypto": ccxt.binance,
        "okx": ccxt.okx,
        "gate": ccxt.gate,
        "kucoin": ccxt.kucoin,
        "mexc": ccxt.mexc,
        "indodax": ccxt.indodax,
    }

def build_public_exchange(exchange_name: str = "binance") -> Any:
    """Build an unauthenticated ccxt client for public price fetches.

    TP/SL reconciliation and position monitoring fetch current prices
    through this helper so symbols on non-binance exchanges (e.g. indodax
    ``GOAT/IDR``) resolve against the right exchange instead of always
    hitting binance. Falls back to binance when the exchange is unknown.
    """
    import ccxt
    exchange_map = _get_exchange_map()
    exchange_class = exchange_map.get((exchange_name or "binance").lower(), ccxt.binance)
    return exchange_class({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
        "timeout": 15000,
    })


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

    Supports Binance, Bybit, Tokocrypto, OKX, Gate, Kucoin, MEXC, and
    Indodax spot markets. Data is returned as a pandas DataFrame with
    normalized column names.
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
                'tokocrypto', 'okx', 'gate', 'kucoin', 'mexc', 'indodax'.
            api_key: API key for authenticated endpoints.
            secret: API secret for authenticated endpoints.

        Raises:
            ValueError: If the exchange is not supported.
        """
        import ccxt
        exchange_name = exchange_name.lower()
        exchange_map = _get_exchange_map()
        exchange_class = exchange_map.get(exchange_name)
        if exchange_class is None:
            msg = (
                f"Unsupported exchange: '{exchange_name}'. "
                f"Supported: {', '.join(exchange_map)}"
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

        import ccxt
        import pandas as pd
        raw: list[list[Any]] = []
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                raw = self.exchange.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=limit,
                )
                last_error = None
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                last_error = exc
                logger.warning(
                    "Exchange fetch attempt %d/3 failed for %s %s: %s",
                    attempt, symbol, timeframe, exc,
                )
                if attempt < 3:
                    import random
                    import time
                    delay = min(30, 2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)
        if last_error is not None:
            raise last_error

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

    def atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate the latest ATR value on fetched data.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.
            period: ATR period (default 14).

        Returns:
            Latest ATR value as a float.
        """
        return IndicatorEngine.atr(df, period=period)

    def market_state(self, df: pd.DataFrame) -> str:
        """Classify market state as trending or sideways.

        Args:
            df: DataFrame returned by ``fetch_ohlcv()``.

        Returns:
            ``"TRENDING"`` or ``"SIDEWAYS"``.
        """
        return MarketStateDetector(
            adx_threshold=CONFIG.get("adx_threshold", 25),
            atr_multiplier=CONFIG.get("atr_multiplier", 0.5),
            volatility_period=CONFIG.get("volatility_lookback", 14),
            compression_lookback=CONFIG.get("price_compression_lookback", 20),
            compression_ratio=CONFIG.get("compression_ratio", 0.3),
        ).detect(df)
