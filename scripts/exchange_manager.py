"""ExchangeManager — owns all exchange providers and manages the active one.

Implements the ``IExchangeManager`` protocol so that existing code continues
to work seamlessly (backward-compatible drop-in replacement).
"""

from __future__ import annotations

from typing import Any, Optional

from scripts.exchange_providers import (
    BaseProvider,
    ExchangeProvider,
    get_provider_class,
    list_supported_exchanges,
)


class ExchangeManager:
    """Manages a collection of exchange providers.

    Typical usage::

        mgr = ExchangeManager(active="binance")
        mgr.get_ticker("BTC/USDT")          # delegates to active provider
        mgr.get_provider("bybit").health_check()

        mgr.set_active("bybit")
        mgr.get_ticker("BTC/USDT")          # now from Bybit
    """

    def __init__(
        self,
        active: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        quote_currency: str = "",
    ) -> None:
        self._providers: dict[str, ExchangeProvider] = {}
        self._active_name: str = active.lower()
        self._lazy_errors: list[str] = []
        # Credentials only apply to the provider matching `active`, since
        # a single API key/secret pair is exchange-specific.
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        # Runtime-overridable quote currency (e.g. IDR for Indodax) —
        # starts as whatever AppConfig/.env said, but can be changed at
        # runtime via `/exchange <name> <quote>` without editing .env.
        self._quote_currency: str = (quote_currency or "USDT").upper()

    # -- Provider access -------------------------------------------------

    def _resolve(self, name: str) -> ExchangeProvider:
        name = name.lower()
        if name not in self._providers:
            try:
                cls = get_provider_class(name)
                if name == self._active_name:
                    self._providers[name] = cls(
                        api_key=self._api_key, api_secret=self._api_secret,
                    )
                else:
                    self._providers[name] = cls()
            except KeyError:
                raise KeyError(f"Unsupported exchange: {name}") from None
        return self._providers[name]

    def get_provider(self, name: Optional[str] = None) -> ExchangeProvider:
        """Return provider for *name*, or active provider if *name* is ``None``."""
        if name is None:
            return self._resolve(self._active_name)
        return self._resolve(name)

    @property
    def active(self) -> ExchangeProvider:
        """Shortcut for ``get_provider()`` (no argument ⇒ active)."""
        return self.get_provider()

    # -- Active exchange management --------------------------------------

    @property
    def name(self) -> str:
        """Return the active exchange name (satisfies ``IExchangeManager``)."""
        return self._active_name

    def set_active(self, name: str) -> None:
        """Switch the active exchange provider (lazy-initialised)."""
        name = name.lower().replace(" ", "")
        self._resolve(name)  # validate existence
        self._active_name = name

    @property
    def quote_currency(self) -> str:
        """Return the currently active quote currency (e.g. USDT, IDR)."""
        return self._quote_currency

    def set_quote_currency(self, quote: str) -> None:
        """Override the quote currency used for scanning/trading at
        runtime (e.g. 'IDR' when switching to Indodax)."""
        self._quote_currency = quote.strip().upper()

    def list_providers(self) -> list[str]:
        """Return sorted list of all supported exchange names."""
        return list_supported_exchanges()

    def list_connected(self) -> dict[str, bool]:
        """Return {exchange_name: health_status} for every loaded provider."""
        result: dict[str, bool] = {}
        for name in list_supported_exchanges():
            try:
                prov = self._resolve(name)
                result[name] = prov.health_check()
            except Exception:
                result[name] = False
        return result

    # -- IExchangeManager delegate methods -------------------------------

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        return self.active.get_ticker(symbol)

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 200,
    ) -> list[list[float]]:
        return self.active.fetch_ohlcv(symbol, timeframe, limit=limit)

    def fetch_balance(self) -> dict[str, Any]:
        return self.active.fetch_balance()

    def get_markets(self) -> list[dict[str, Any]]:
        return self.active.get_markets()

    def health_check(self) -> bool:
        return self.active.health_check()

    def load_markets(self) -> dict[str, Any]:
        return self.active.load_markets()

    def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict[str, Any]:
        return self.active.fetch_tickers(symbols)

    def has(self, feature: str) -> bool:
        return self.active.has(feature)
