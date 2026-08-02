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


class ExchangeAuthError(Exception):
    """Raised when a live exchange call fails despite credentials being set.

    Callers (wallet adapters, live executor) MUST treat this as fatal and
    must NOT fall back to interpreting it as a zero/empty balance — doing
    so silently would let the bot size or place orders against a wrong
    balance.
    """


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
        """Fetch account balance.

        Raises ``ExchangeAuthError`` if credentials are set but the call
        fails (auth error, network, etc.) — never silently returns an
        empty/zero balance in that case. Returns ``{}`` only when no
        credentials were provided (e.g. paper/scanning use).
        """
        ...

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Fetch the current state of a previously-submitted order.

        Used for reconciliation — a ``create_order()`` response is not
        guaranteed final, so callers poll this until the order reaches a
        terminal state. Same error-handling contract as
        ``fetch_balance()``: raises ``ExchangeAuthError`` when
        credentials are set but the call fails, returns ``{}`` only when
        no credentials are configured.
        """
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

    def has_credentials(self) -> bool:
        """True if this provider was constructed with API key + secret."""
        ...

    def fetch_api_key_permissions(self) -> dict[str, Any]:
        """Best-effort fetch of THIS API key's own granted permissions.

        Deliberately separate from ``fetch_balance()``'s raw ``info``
        dict: on Binance (and Binance-derived exchanges like
        Tokocrypto), the account endpoint's ``canWithdraw`` /
        ``canTrade`` / ``canDeposit`` flags reflect the *account's*
        overall capability, not what this specific API key is actually
        allowed to do — they are commonly ``true`` even for a
        read-only key. See:
        https://dev.binance.vision/t/how-to-validate-an-api-key-permissions/1519

        Returns ``{}`` when the exchange/ccxt build doesn't expose a
        dedicated permissions endpoint — callers must treat that as
        "unknown", never as "no restrictions".
        """
        ...

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        """Return the ccxt ``params`` dict to tag an order with a client id.

        The correct param name differs per exchange (e.g. Binance wants
        ``newClientOrderId``, others accept the unified ``clientOrderId``).
        """
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

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self._instance: Any = None
        self._markets_cache: dict[str, Any] = {}
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""

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
            if self._api_key and self._api_secret:
                kwargs["apiKey"] = self._api_key
                kwargs["secret"] = self._api_secret
            kwargs.update(self.CCXT_KWARGS)
            self._instance = cls(kwargs)
        return self._instance

    def has_credentials(self) -> bool:
        """True if this provider was constructed with API key + secret."""
        return bool(self._api_key and self._api_secret)

    def fetch_api_key_permissions(self) -> dict[str, Any]:
        """Best-effort fetch of this API key's own granted permissions.

        Default implementation tries Binance's dedicated
        ``GET /sapi/v1/account/apiRestrictions`` endpoint (exposed by
        ccxt as the implicit method ``sapiGetAccountApiRestrictions``),
        which is the ONLY reliable source for a key-specific
        ``enableWithdrawals`` flag. Exchanges that don't expose this
        (or older ccxt builds without the implicit method) return
        ``{}`` — see the Protocol docstring for why this must NOT be
        treated as "no restrictions".
        """
        try:
            ex = self._get_exchange()
            method = getattr(ex, "sapiGetAccountApiRestrictions", None)
            if method is None:
                return {}
            return dict(method())
        except Exception:
            return {}

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        """Default: unified ccxt ``clientOrderId`` param.

        Override per-exchange if the API needs a different key name
        (see ``BinanceProvider``) or has no client-order-id concept at
        all (see ``IndodaxProvider``).
        """
        return {"clientOrderId": client_order_id}

    def market_buy_requires_price(self) -> bool:
        """True when the exchange API requires a ``price`` argument to
        submit a MARKET BUY order.

        Most exchanges ignore price for market orders, but some (e.g.
        Indodax) compute the quote amount to spend as ``amount * price``
        and reject market buys without it. Callers must pass the resolved
        price through to ``create_order`` only when this is True, because
        on Binance & friends a stray price converts a market BUY into a
        ``quoteOrderQty`` spend instead of a base-quantity order.
        """
        return False

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
        except Exception as exc:
            if self.has_credentials():
                # We have API keys — a failure here means auth, permissions,
                # or connectivity are broken. Surface it loudly instead of
                # returning {} (which callers could misread as zero balance).
                raise ExchangeAuthError(
                    f"{self.name}: fetch_balance failed with credentials "
                    f"set — {exc}"
                ) from exc
            # No credentials configured (paper/scanning use) — {} is the
            # expected, harmless result.
            return {}

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        try:
            return dict(self._get_exchange().fetch_order(order_id, symbol))
        except Exception as exc:
            if self.has_credentials():
                raise ExchangeAuthError(
                    f"{self.name}: fetch_order({order_id}) failed — {exc}"
                ) from exc
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

    # -- Order precision (required before submitting live orders) --------

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round *amount* to the symbol's lot-size step.

        Exchanges reject orders whose amount doesn't match the market's
        step size. Falls back to the raw amount if markets can't be loaded.
        """
        try:
            ex = self._get_exchange()
            if not ex.markets:
                ex.load_markets()
            return float(ex.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        """Round *price* to the symbol's tick size (see ``amount_to_precision``)."""
        try:
            ex = self._get_exchange()
            if not ex.markets:
                ex.load_markets()
            return float(ex.price_to_precision(symbol, price))
        except Exception:
            return price


# ======================================================================
#  Provider implementations
# ======================================================================


class BinanceProvider(BaseProvider):
    CCXT_NAME = "binance"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        # Binance's spot API expects newClientOrderId, not clientOrderId.
        return {"newClientOrderId": client_order_id}


class BybitProvider(BaseProvider):
    CCXT_NAME = "bybit"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}


class TokocryptoProvider(BaseProvider):
    """Tokocrypto uses Binance's API (same ccxt class).

    NOTE: this reuses BinanceProvider's auth flow as-is. If Tokocrypto's
    API ever requires extra params (e.g. a broker/UID field) beyond plain
    apiKey/secret, this class will need its own CCXT_KWARGS override —
    not yet implemented since Tokocrypto isn't live-tested.
    """
    CCXT_NAME = "binance"
    CCXT_KWARGS = {"options": {"defaultType": "spot"}}

    @property
    def name(self) -> str:
        return "tokocrypto"

    @property
    def ccxt_name(self) -> str:
        return "binance"

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        return {"newClientOrderId": client_order_id}


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


class IndodaxProvider(BaseProvider):
    """Indodax (Indonesia) — IDR-quoted spot pairs only (e.g. BTC/IDR).

    Unlike Binance/Bybit/OKX etc., Indodax has no spot-vs-futures split,
    so no ``defaultType`` option is needed. Not yet live-tested against
    the real API in this codebase — verify with a read-only (view+trade,
    NO withdrawal) API key on a small balance before trading real funds.
    See: https://github.com/btcid/indodax-official-api-docs
    """
    CCXT_NAME = "indodax"

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        # Indodax has no client-order-id concept. Its private trade
        # endpoint signs the ENTIRE request body, so sending an
        # unsupported param risks the order being rejected server-side —
        # return no params rather than leak an unknown field in.
        return {}

    def market_buy_requires_price(self) -> bool:
        # Indodax's API sizes a market BUY by the quote (IDR) amount; ccxt
        # computes that cost as amount × price and raises InvalidOrder
        # when price is missing. SELL is sized by base quantity and needs
        # no price.
        return True


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
    IndodaxProvider,
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
