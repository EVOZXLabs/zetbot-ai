"""Exchange provider adapters for all supported exchanges.

Each provider is a tiny subclass of ``BaseProvider`` that configures
the CCXT exchange class name and optional overrides.

Usage::

    provider = BinanceProvider()
    ticker = provider.get_ticker("BTC/USDT")
    ohlcv = provider.fetch_ohlcv("BTC/USDT")
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import random
import time
from typing import Any, Callable, Optional, Protocol, TypeVar, runtime_checkable

import ccxt  # noqa: PLC0415
import requests

_log = logging.getLogger("ZetBot")

T = TypeVar("T")

# ---------------------------------------------------------------------------
#  Exchange API retry helper
# ---------------------------------------------------------------------------

_MAX_EXCHANGE_RETRIES = 3
_RETRY_BASE_DELAY = 2.0    # seconds; doubles each attempt (2s, 4s, 8s)
_RETRY_MAX_DELAY = 30.0    # cap backoff at 30s


def exchange_call_with_retry(
    fn: Callable[[], T],
    label: str = "",
    retries: int = _MAX_EXCHANGE_RETRIES,
    record_failure: Optional[Callable[[], None]] = None,
    exchange: str = "",
) -> T:
    """Call ``fn()`` with exponential-backoff retry on transient exchange errors.

    Retries up to ``retries`` times on ``ccxt.NetworkError``,
    ``ccxt.RequestTimeout``, and ``ccxt.ExchangeNotAvailable``.
    Permanent errors (``ccxt.AuthenticationError``, bad-symbol, etc.)
    are re-raised immediately without retrying.

    Every failure is logged with the exchange name, the method/symbol
    label, and the CCXT exception CLASS (NetworkError / RequestTimeout /
    ExchangeNotAvailable / …) so the root cause is visible in the log
    without digging through tracebacks.

    Args:
        fn: Zero-argument callable that performs the exchange call.
        label: Human-readable label for log messages (method + symbol).
        retries: Maximum retry attempts (default 3).
        record_failure: Optional callback called exactly once per failed
            attempt so callers can drive ``SafeGuard.record_exchange_failure()``.
            Total calls == number of attempts that actually raised an exception
            (never more).
        exchange: Exchange name (e.g. ``"binance"``) for root-cause logging.

    Raises:
        The last exception from ``fn`` when all retries are exhausted.
    """
    TRANSIENT = (
        ccxt.NetworkError,
        ccxt.RequestTimeout,
        ccxt.ExchangeNotAvailable,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    )
    where = f"{exchange + ': ' if exchange else ''}{label or fn.__name__}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except ccxt.AuthenticationError:
            # Permanent — don't retry, bubble up immediately.
            raise
        except TRANSIENT as exc:
            last_exc = exc
            if record_failure is not None:
                try:
                    record_failure()
                except Exception:
                    pass
            if attempt < retries:
                _log.info(
                    "Exchange call '%s' failed (attempt %d/%d): "
                    "%s — %s — retrying",
                    where, attempt, retries, type(exc).__name__, exc,
                )
                delay = min(
                    _RETRY_MAX_DELAY,
                    _RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1),
                )
                time.sleep(delay)
            else:
                _log.warning(
                    "Exchange call '%s' failed after %d attempts: "
                    "%s — %s",
                    where, retries, type(exc).__name__, exc,
                )
        except Exception as exc:
            # Non-transient exchange error — log and re-raise immediately.
            _log.warning(
                "Exchange call '%s' non-transient error: %s — %s",
                where, type(exc).__name__, exc,
            )
            raise

    # All retries exhausted — re-raise the last transient exception.
    # record_failure() was already called inside the loop for every failed
    # attempt; do NOT call it again here to avoid a double-count on the
    # final attempt.
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
#  Market-availability guards (maintenance / suspension)
# ---------------------------------------------------------------------------

# Error phrases that indicate the exchange will not accept orders for a
# symbol right now because it is under maintenance / suspended. Matched
# case-insensitively against order-rejection error text so such failures
# are surfaced clearly instead of being retried pointlessly.
_MAINTENANCE_PHRASES = (
    "maintenance",
    "suspended",
    "not trading",
    "under maintenance",
    "temporarily unavailable",
    "suspend trading",
    "market not available",
    "inactive market",
)


def is_market_tradeable(provider: Any, symbol: str) -> bool:
    """Whether ``symbol`` is currently tradeable on ``provider``'s exchange.

    Returns ``True`` unless the exchange explicitly reports the market as
    inactive — e.g. Indodax marks pairs under maintenance with
    ``active: False`` (from the API's ``is_maintenance`` flag). When the
    market status cannot be determined (markets failed to load, the
    exchange doesn't report activity), the order is allowed through and a
    real rejection is surfaced clearly by the caller instead.
    """
    try:
        markets = provider.load_markets()
    except Exception:
        return True
    if not markets:
        return True
    market = markets.get(symbol)
    if market is None:
        return False
    active = market.get("active")
    if active is None:
        return True
    return bool(active)


def looks_like_maintenance_error(text: str) -> bool:
    """True when ``text`` looks like an order rejection due to market
    maintenance / suspension rather than a transient network failure."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _MAINTENANCE_PHRASES)


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

    def fetch_my_trades(
        self, symbol: str, *, since: int | None = None, until: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the account's fills for ``symbol`` (newest first).

        Used to RECOVER the average entry price of a position whose entry
        the bot never recorded (e.g. a holding that existed on the exchange
        before the bot was armed, or a manual buy). Returns a list of
        unified trade dicts with at least ``price``, ``qty``, ``side`` and
        ``timestamp``. Empty list when the exchange exposes no trade
        history (or no credentials). May page backwards through multiple
        windows to honour exchange range limits.
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

# Process-wide CCXT instances, keyed by (CCXT_NAME, api_key, api_secret).
# Every pipeline stage (scanner/decision/risk/…) constructs its own
# provider, which used to build a FRESH CCXT instance each time — and
# Indodax's ``_get_exchange`` forced ``load_markets()`` on every call,
# re-hitting ``/api/pairs`` several times per pipeline and frequently
# timing out (seen as ``Exchange call 'indodax: fetch_markets' failed
# (attempt 1/3)`` warnings). Reusing one instance per credential set
# means markets load once per process and the warning disappears.
_EXCHANGE_INSTANCES: dict[tuple[str, str, str], Any] = {}

# TTL cache of raw ``fetch_markets()`` output per provider name. The
# scanner needs the raw market array every cycle; re-fetching it from
# the wire each pipeline is wasteful and a frequent transient-timeout
# source. Fall back to the last good list when a refresh fails.
_MARKETS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_MARKETS_TTL_SECONDS = 1800.0


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
            # Share one CCXT instance per (exchange, credentials) across the
            # whole process: every pipeline stage constructs its own provider
            # object, and re-creating the instance re-fetches markets every
            # time (the recurring Indodax /api/pairs timeout warning).
            key = (self.__class__.__name__, self._api_key, self._api_secret)
            if key not in _EXCHANGE_INSTANCES:
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
                _EXCHANGE_INSTANCES[key] = cls(kwargs)
            self._instance = _EXCHANGE_INSTANCES[key]
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

    def market_order_params(self) -> dict[str, Any]:
        """Extra ``params`` every MARKET order must carry on this exchange.

        Returns ``{}`` by default — most exchanges infer the order type
        from ccxt's ``type="market"`` argument. Exchanges that need an
        explicit type marker on the wire override this (see
        ``IndodaxProvider``).
        """
        return {}

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
            return exchange_call_with_retry(
                lambda: dict(self._get_exchange().fetch_ticker(symbol)),
                label=f"get_ticker({symbol})",
                exchange=self.name,
            )
        except Exception:
            return {}

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 200,
    ) -> list[list[float]]:
        try:
            result = exchange_call_with_retry(
                lambda: self._get_exchange().fetch_ohlcv(
                    symbol, timeframe, limit=limit,
                ),
                label=f"fetch_ohlcv({symbol})",
                exchange=self.name,
            )
            return [list(map(float, r)) for r in result]
        except Exception:
            return []

    def fetch_balance(self) -> dict[str, Any]:
        try:
            return exchange_call_with_retry(
                lambda: dict(self._get_exchange().fetch_balance()),
                label="fetch_balance",
                exchange=self.name,
            )
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
            self._markets_cache = dict(exchange_call_with_retry(
                lambda: self._get_exchange().load_markets(),
                label="load_markets",
                exchange=self.name,
            ))
            return self._markets_cache
        except Exception:
            return {}

    def fetch_markets(self) -> list[dict[str, Any]]:
        """Fetch the full market list with the shared retry/backoff wrapper.

        ``MarketScanner`` needs the raw market array (to filter by quote
        currency / spot / active), so it cannot use the dict-shaped
        ``load_markets()``. Callers MUST go through this method rather than
        ``self._get_exchange().fetch_markets()`` directly: the latter has no
        retry, so the very first call after a cold start (DNS/TLS/connect to
        e.g. Indodax ``/api/pairs``) would fail the whole scan and only
        succeed on the second run once the connection is warm. Works for
        every configured exchange (Binance, Tokocrypto, OKX, Indodax, …)
        because it delegates through ``self.CCXT_NAME``.

        Refreshes from the wire at most every ``_MARKETS_TTL_SECONDS`` per
        provider; a refresh failure falls back to the last good list (a
        stale market list beats a failed scan), and the very first success
        primes the cache so the recurring cold-start timeout warning stops
        reappearing on every pipeline.
        """
        now = time.monotonic()
        cached = _MARKETS_CACHE.get(self.name)
        if cached and now - cached[0] < _MARKETS_TTL_SECONDS:
            return cached[1]
        try:
            markets = list(exchange_call_with_retry(
                lambda: self._get_exchange().fetch_markets(),
                label="fetch_markets",
                exchange=self.name,
            ))
            _MARKETS_CACHE[self.name] = (now, markets)
            return markets
        except Exception:
            if cached:
                return cached[1]
            return []

    def fetch_my_trades(
        self, symbol: str, *, since: int | None = None, until: int | None = None,
    ) -> list[dict[str, Any]]:
        """Default: unified CCXT ``fetch_my_trades`` when the exchange
        supports it; otherwise no trade history (empty list). Exchanges
        without it (Indodax) override this method."""
        if not self.has_credentials():
            return []
        ex = self._get_exchange()
        if not ex.has.get("fetchMyTrades"):
            return []
        try:
            params: dict[str, Any] = {}
            result = exchange_call_with_retry(
                lambda: ex.fetch_my_trades(symbol, since, limit=1000, params=params),
                label=f"fetch_my_trades({symbol})",
                exchange=self.name,
            )
            return [
                {
                    "price": float(t.get("price", 0.0)),
                    "qty": float(t.get("amount", 0.0)),
                    "cost": float(t.get("cost", 0.0)),
                    "side": t.get("side", ""),
                    "fee": t.get("fee", {}) or {},
                    "timestamp": t.get("timestamp") or 0,
                }
                for t in result
            ]
        except Exception:
            return []

    def fetch_tickers(
        self, symbols: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        try:
            return exchange_call_with_retry(
                lambda: dict(self._get_exchange().fetch_tickers(symbols)),
                label="fetch_tickers",
                exchange=self.name,
            )
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

    def _get_exchange(self) -> Any:
        ex = super()._get_exchange()
        # ccxt 4.5.x collapses Indodax pair ids to "{base}{quote}"
        # (``btcidr``), but the private /tapi endpoint only accepts the
        # underscore form (``btc_idr``) — every order, cancel and order
        # lookup sent the id-less form and Indodax answered
        # ``{"success":0,"error":"Invalid pair.}`` for ALL symbols
        # (verified live: pair=btcidr → error, pair=btc_idr → success).
        # Rewrite the cached market ids once so every ccxt call (public
        # tickers accept both forms; private calls need the underscore)
        # uses the correct pair id.
        if not ex.markets:
            # Markets not loaded yet (cold start) — fetch them once; the
            # shared process-wide instance keeps them cached afterwards.
            try:
                ex.load_markets()
            except Exception:
                return ex
        self._rewrite_market_ids(ex)

        # Indodax signs every private request with a nonce derived from
        # the LOCAL clock (``nonce = milliseconds() - timeDifference``).
        # If the device clock drifts from Indodax server time, private
        # calls fail with ``Invalid timestamp`` — sync the offset once
        # per process so nonces are derived from server time instead.
        if not getattr(ex, "_zetbot_time_synced", False):
            try:
                ex.load_time_difference()
                ex._zetbot_time_synced = True
            except Exception:
                # Non-fatal: retried on the next _get_exchange() call.
                pass

        # Patch load_markets so the rewrite survives ccxt internal
        # re-fetches (e.g. create_order calls load_markets which would
        # overwrite our underscore pair ids back to the bare form).
        # ONLY ONCE per exchange instance — _get_exchange() is called on
        # every order/ticker/balance path, and each re-wrap stacks another
        # closure over the previous one, growing the call depth until
        # Python hits RecursionError ("maximum recursion depth exceeded"
        # in load_markets — observed live after ~months of uptime, which
        # then failed EVERY sell).
        if getattr(ex, "_zetbot_markets_patched", False):
            return ex
        _orig_load = ex.load_markets
        _provider = self

        def _patched_load_markets(*args, **kwargs):
            result = _orig_load(*args, **kwargs)
            _provider._rewrite_market_ids(ex)
            return result

        ex.load_markets = _patched_load_markets
        ex._zetbot_markets_patched = True
        return ex

    @staticmethod
    def _rewrite_market_ids(ex: Any) -> None:
        if not ex.markets:
            return
        for market in ex.markets.values():
            base = market.get("base")
            quote = market.get("quote")
            if base and quote:
                market["id"] = f"{base}_{quote}".lower()

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        # Indodax has no client-order-id concept. Its private trade
        # endpoint signs the ENTIRE request body, so sending an
        # unsupported param risks the order being rejected server-side —
        # return no params rather than leak an unknown field in.
        return {}

    def market_order_params(self) -> dict[str, Any]:
        # Indodax's /trade endpoint defaults ``order_type`` to ``limit``
        # when the field is absent (API change 10 Sep 2022). A market
        # order sent without it is therefore treated as a LIMIT order —
        # and a limit buy sized by quote (``idr``) amount is rejected
        # server-side ("Request will be rejected if you send BUY order
        # request with both idr set & order_type set to LIMIT"), which
        # surfaced as an opaque FAILED on every live BUY. Declare the
        # market type explicitly on the wire.
        return {"order_type": "market"}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round *amount* to the lot step Indodax actually accepts.

        Indodax reports ``volume_precision: 0`` for every pair (its
        /trade endpoint always answers ``amount can't be in decimal.``
        when a non-whole number is sent) but the true per-pair rule is
        driven by ``price_precision``: pairs quoted with a sub-1 IDR
        price step (e.g. GPS/IDR price_precision=0.001) accept ONLY
        whole-coin amounts, while pairs with price_precision >= 1 IDR
        (BTC/IDR=1000, DOGE/IDR=1) accept fractional amounts (verified
        live: a GPS SELL of 203.1 → ``amount can't be in decimal.``,
        while BTC sells with decimals fill fine). ccxt's own
        ``precision.amount`` is a hardcoded 1e-8 for every pair and
        would let the decimal slip through to the exchange, where it is
        rejected and the TP/SL exit never executes.
        """
        try:
            ex = self._get_exchange()
            if not ex.markets:
                ex.load_markets()
            market = ex.markets.get(symbol)
            if market is not None:
                info = market.get("info") or {}
                price_precision = float(info.get("price_precision") or 1.0)
                if price_precision < 1.0:
                    # Whole-coin pairs: never send a fractional amount.
                    # Floor (not round) so a TP slice can never oversell
                    # beyond the remaining position.
                    return float(math.floor(amount))
            return float(ex.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    def market_buy_requires_price(self) -> bool:
        # Indodax's API sizes a market BUY by the quote (IDR) amount; ccxt
        # computes that cost as amount × price and raises InvalidOrder
        # when price is missing. SELL is sized by base quantity and needs
        # no price.
        return True

    def fetch_my_trades(
        self, symbol: str, *, since: int | None = None, until: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recover the account's fills from ``GET /api/v2/myTrades``.

        The legacy ``tradeHistory`` /tapi method was decommissioned on
        2026-04-07; the v2 endpoint is HMAC-SHA512-signed over the query
        string and caps each query to a 7-day window (default last 24h).
        This implementation pages backwards in 7-day windows (up to
        ``_MY_TRADES_MAX_WINDOWS``) so entry prices can be recovered even
        for holdings bought before the bot's own ledger began. Returns
        unified trade dicts (newest first, per API).
        """
        if not self.has_credentials():
            return []
        ex = self._get_exchange()
        market = ex.markets.get(symbol)
        if market is None:
            return []
        base_id = str(market.get("base", "")).lower()
        quote_id = str(market.get("quote", "")).lower()
        if not base_id or not quote_id:
            return []
        endpoint_symbol = f"{base_id}{quote_id}"

        now_ms = int(time.time() * 1000)
        window_ms = 7 * 24 * 3600 * 1000
        start = since or (now_ms - window_ms)
        end = until or now_ms
        if end - start > window_ms:
            start = end - window_ms

        trades: list[dict[str, Any]] = []
        for _ in range(_MY_TRADES_MAX_WINDOWS):
            params: dict[str, Any] = {
                "symbol": endpoint_symbol,
                "limit": 1000,
                "sort": "desc",
                "timestamp": int(time.time() * 1000),
                "recvWindow": 5000,
                "startTime": start,
                "endTime": end,
            }
            query = "&".join(f"{k}={v}" for k, v in params.items())
            sign = hmac.new(
                self._api_secret.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha512,
            ).hexdigest()
            try:
                resp = exchange_call_with_retry(
                    lambda: requests.get(
                        "https://tapi.indodax.com/api/v2/myTrades",
                        params=params,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "X-APIKEY": self._api_key,
                            "Sign": sign,
                        },
                        timeout=15,
                    ),
                    label=f"fetch_my_trades({symbol})",
                    exchange=self.name,
                )
            except Exception:
                break
            try:
                data = resp.json().get("data", []) or []
            except Exception:
                break
            trades.extend(
                {
                    "price": float(t.get("price") or 0.0),
                    "qty": float(t.get("qty") or 0.0),
                    "cost": float(t.get("quoteQty") or 0.0),
                    "side": "buy" if t.get("isBuyer") else "sell",
                    "fee": float(t.get("commission") or 0.0),
                    "fee_asset": t.get("commissionAsset", ""),
                    "timestamp": int(t.get("time") or 0),
                }
                for t in data
            )
            if len(data) < 1000:
                break
            oldest = min((t["timestamp"] for t in data), default=0)
            if not oldest:
                break
            end = oldest
            start = end - window_ms
        return trades


# ======================================================================
#  Provider registry (auto-discovered)
# ======================================================================

# Max 7-day windows to walk back when recovering trade history for an
# unknown-entry position (28 days). Bounded so an ancient holding cannot
# trigger unbounded API paging.
_MY_TRADES_MAX_WINDOWS = 4

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
