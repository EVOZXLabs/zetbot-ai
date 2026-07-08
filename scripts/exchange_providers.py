"""Exchange provider adapters for all supported exchanges.

Each provider is a tiny subclass of ``BaseProvider`` that configures
the CCXT exchange class name and optional overrides.

Usage::

    provider = BinanceProvider()
    ticker = provider.get_ticker("BTC/USDT")
    ohlcv = provider.fetch_ohlcv("BTC/USDT")
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

import ccxt  # noqa: PLC0415


# ======================================================================
#  ExchangeProvider Protocol
# ======================================================================


@runtime_checkable
class ExchangeProvider(Protocol):
    """Interface that every exchange provider must satisfy."""

    @property
    def name(self) -> str:
        """Exchange identifier, e.g. ``"binance"``."""
        ...

    @property
    def ccxt_name(self) -> str:
        """CCXT module attribute name, e.g. ``"binance"``."""
        ...

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker for a symbol."""
        ...

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 200,
    ) -> list[list[float]]:
        """Fetch OHLCV candles."""
        ...

    def fetch_balance(self) -> dict[str, Any]:
        """Fetch account balance (empty in paper mode)."""
        ...

    def get_markets(self) -> list[dict[str, Any]]:
        """Return all available markets."""
        ...

    def health_check(self) -> bool:
        """Ping the exchange API to verify connectivity."""
        ...

    def load_markets(self) -> dict[str, Any]:
        """Load and return markets dict."""
        ...

    def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict[str, Any]:
        """Fetch tickers for given symbols (or all)."""
        ...

    def has(self, feature: str) -> bool:
        """Check if exchange supports a feature (e.g. 'fetchOHLCV')."""
        ...


# ======================================================================
#  BaseProvider — shared CCXT logic
# ======================================================================


class BaseProvider:
    """Base exchange provider with shared CCXT instance management.

    Subclasses set ``CCXT_NAME`` and optionally ``CCXT_KWARGS``.
    """

    CCXT_NAME: str = ""
    CCXT_KWARGS: dict[str, Any] = {}

    def __init__(self) -> None:
        self._instance: Any = None
        self._markets_cache: dict[str, Any] = {}

    # -- CCXT instance ---------------------------------------------------

    def _get_exchange(self) -> Any:
        if self._instance is None:
            cls = getattr(ccxt, self.CCXT_NAME, None)
            if cls is None:
                raise ValueError(
                    f"CCXT has no exchange '{self.CCXT_NAME}'"
                )
            kwargs: dict[str, Any] = {
                "enableRateLimit": True,
                "timeout": 15000,
            }
            kwargs.update(self.CCXT_KWARGS)
            self._instance = cls(kwargs)
        return self._instance

    # -- Properties ------------------------------------------------------

    @property
    def name(self) -> str:
        return self.CCXT_NAME

    @property
    def ccxt_name(self) -> str:
        return self.CCXT_NAME

    # -- Public API ------------------------------------------------------

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        try:
            return dict(self._get_exchange().fetch_ticker(symbol))
        except Exception:
            return {}

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 200,
    ) -> list[list[float]]:
        try:
            result = self._get_exchange().fetch_ohlcv(
                symbol, timeframe, limit=limit,
            )
            return [list(map(float, r)) for r in result]
        except Exception:
            return []

    def fetch_balance(self) -> dict[str, Any]:
        try:
            return dict(self._get_exchange().fetch_balance())
        except Exception:
            return {}

    def get_markets(self) -> list[dict[str, Any]]:
        try:
            ex = self._get_exchange()
            if ex.markets:
                return list(ex.markets.values())
            ex.load_markets()
            return list(ex.markets.values())
        except Exception:
            return []

    def health_check(self) -> bool:
        try:
            self._get_exchange().fetch_time()
            return True
        except Exception:
            return False

    def load_markets(self) -> dict[str, Any]:
        try:
            self._markets_cache = dict(self._get_exchange().load_markets())
            return self._markets_cache
        except Exception:
            return {}

    def fetch_tickers(
        self, symbols: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        try:
            return dict(self._get_exchange().fetch_tickers(symbols))
        except Exception:
            return {}

    def has(self, feature: str) -> bool:
        try:
            return bool(self._get_exchange().has.get(feature, False))
        except Exception:
            return False


# ======================================================================
#  Provider implementations
# ======================================================================


class BinanceProvider(BaseProvider):
    CCXT_NAME = "binance"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class BybitProvider(BaseProvider):
    CCXT_NAME = "bybit"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class TokocryptoProvider(BaseProvider):
    """Tokocrypto uses Binance's API (same ccxt class)."""
    CCXT_NAME = "binance"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}

    @property
    def name(self) -> str:
        return "tokocrypto"

    @property
    def ccxt_name(self) -> str:
        return "binance"


class OKXProvider(BaseProvider):
    CCXT_NAME = "okx"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class GateProvider(BaseProvider):
    CCXT_NAME = "gate"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class KucoinProvider(BaseProvider):
    CCXT_NAME = "kucoin"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class MEXCProvider(BaseProvider):
    CCXT_NAME = "mexc"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


# ======================================================================
#  Provider registry (auto-discovered)
# ======================================================================

_BUILTIN_PROVIDERS: list[type[BaseProvider]] = [
    BinanceProvider,
    BybitProvider,
    TokocryptoProvider,
    OKXProvider,
    GateProvider,
    KucoinProvider,
    MEXCProvider,
]


def get_provider_class(name: str) -> type[BaseProvider]:
    """Return the provider class for a given exchange name.

    Raises ``KeyError`` if not found.
    """
    name = name.lower().replace(" ", "")
    mapping: dict[str, type[BaseProvider]] = {}
    for cls in _BUILTIN_PROVIDERS:
        inst = cls()
        mapping[inst.name] = cls
    if name in mapping:
        return mapping[name]
    raise KeyError(f"Unsupported exchange: {name}")


def list_supported_exchanges() -> list[str]:
    """Return sorted list of supported exchange names."""
    names: set[str] = set()
    for cls in _BUILTIN_PROVIDERS:
        names.add(cls().name)
    return sorted(names)
