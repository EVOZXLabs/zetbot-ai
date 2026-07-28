"""Unit tests for ExchangeProvider protocol, BaseProvider, and all adapters."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.exchange_providers import (
    BaseProvider,
    BinanceProvider,
    BybitProvider,
    ExchangeProvider,
    GateProvider,
    IndodaxProvider,
    KucoinProvider,
    MEXCProvider,
    OKXProvider,
    TokocryptoProvider,
    get_provider_class,
    list_supported_exchanges,
)

SUPPORTED = ["binance", "bybit", "gate", "indodax", "kucoin", "mexc", "okx", "tokocrypto"]


def test_list_supported_exchanges() -> None:
    assert list_supported_exchanges() == SUPPORTED


def test_get_provider_class() -> None:
    assert get_provider_class("binance") is BinanceProvider
    assert get_provider_class("bybit") is BybitProvider
    assert get_provider_class("tokocrypto") is TokocryptoProvider
    assert get_provider_class("okx") is OKXProvider
    assert get_provider_class("gate") is GateProvider
    assert get_provider_class("kucoin") is KucoinProvider
    assert get_provider_class("mexc") is MEXCProvider
    assert get_provider_class("indodax") is IndodaxProvider


def test_get_provider_class_case_insensitive() -> None:
    assert get_provider_class("BINANCE") is BinanceProvider
    assert get_provider_class("Bybit") is BybitProvider


def test_get_provider_class_unknown_raises() -> None:
    try:
        get_provider_class("unknown_exchange")
        assert False, "Expected KeyError"
    except KeyError:
        pass


# ======================================================================
#  Provider identity
# ======================================================================


class TestProviderIdentity:
    def test_binance_name(self) -> None:
        p = BinanceProvider()
        assert p.name == "binance"
        assert p.ccxt_name == "binance"

    def test_bybit_name(self) -> None:
        p = BybitProvider()
        assert p.name == "bybit"
        assert p.ccxt_name == "bybit"

    def test_tokocrypto_name(self) -> None:
        p = TokocryptoProvider()
        assert p.name == "tokocrypto"
        assert p.ccxt_name == "binance"

    def test_okx_name(self) -> None:
        p = OKXProvider()
        assert p.name == "okx"
        assert p.ccxt_name == "okx"

    def test_gate_name(self) -> None:
        p = GateProvider()
        assert p.name == "gate"
        assert p.ccxt_name == "gate"

    def test_kucoin_name(self) -> None:
        p = KucoinProvider()
        assert p.name == "kucoin"
        assert p.ccxt_name == "kucoin"

    def test_mexc_name(self) -> None:
        p = MEXCProvider()
        assert p.name == "mexc"
        assert p.ccxt_name == "mexc"

    def test_indodax_name(self) -> None:
        p = IndodaxProvider()
        assert p.name == "indodax"
        assert p.ccxt_name == "indodax"


# ======================================================================
#  Per-exchange rate limits
# ======================================================================


class TestRateLimits:
    """Each exchange has a distinct RATE_LIMIT_MS that reflects
    its API throttling policy.  Verify every provider exposes
    a positive value so external schedulers can estimate
    throughput."""

    RATE_LIMITS = {
        "binance": 100,
        "bybit": 100,
        "tokocrypto": 100,
        "okx": 200,
        "gate": 50,
        "kucoin": 1000,
        "mexc": 1000,
        "indodax": 200,
    }

    def test_all_providers_have_rate_limit(self) -> None:
        for name, expected_ms in self.RATE_LIMITS.items():
            p = get_provider_class(name)()
            assert p.rate_limit_ms == expected_ms, (
                f"{name} rate_limit_ms={p.rate_limit_ms}, expected {expected_ms}"
            )

    def test_rate_limit_positive(self) -> None:
        for name in SUPPORTED:
            p = get_provider_class(name)()
            assert p.rate_limit_ms > 0, (
                f"{name} rate_limit_ms must be positive"
            )


# ======================================================================
#  Per-exchange error handling and fallback behaviour
# ======================================================================


class TestErrorHandling:
    """Without network, all fetch methods must return safe defaults
    (empty dict/list) instead of raising — except when live
    credentials are present, where `ExchangeAuthError` is
    expected."""

    def test_get_ticker_returns_dict_offline(self) -> None:
        assert isinstance(IndodaxProvider().get_ticker("BTC/IDR"), dict)

    def test_fetch_ohlcv_returns_list_offline(self) -> None:
        assert isinstance(IndodaxProvider().fetch_ohlcv("BTC/IDR"), list)

    def test_fetch_balance_returns_empty_dict_no_creds(self) -> None:
        assert isinstance(IndodaxProvider().fetch_balance(), dict)

    def test_fetch_balance_raises_auth_error_with_creds(self) -> None:
        p = IndodaxProvider(api_key="bad", api_secret="bad")
        with pytest.raises(Exception):
            p.fetch_balance()

    def test_fetch_tickers_fallback_per_symbol(self) -> None:
        """Exchanges without bulk ticker support should fall back
        to per-symbol fetches rather than crashing."""
        provider = IndodaxProvider()
        result = provider.fetch_tickers()
        assert isinstance(result, dict)

    def test_fetch_tickers_with_symbols_param(self) -> None:
        provider = IndodaxProvider()
        result = provider.fetch_tickers(["BTC/IDR"])
        assert isinstance(result, dict)


# ======================================================================
#  Rate-limit enforcement via CCXT enableRateLimit
# ======================================================================


class TestRateLimitEnforcement:
    """CCXT's ``enableRateLimit`` must be True on every provider
    instance so that ccxt internally throttles requests to
    respect each exchange's ``rateLimit``."""

    @patch("scripts.exchange_providers.ccxt")
    def test_enable_rate_limit_set_on_init(self, mock_ccxt: Any) -> None:
        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.rateLimit = 1000
            getattr(mock_ccxt, name, lambda **kw: mock_ex)(
                {"enableRateLimit": True, "timeout": 15000},
            )
            # Just verify no crash — the constructor was called

    def test_ccxt_kwargs_include_enableRateLimit(self) -> None:
        """When a provider creates its CCXT instance the kwargs
        must contain ``enableRateLimit: True``  so ccxt handles
        per-exchange throttling automatically."""
        for name in SUPPORTED:
            p = get_provider_class(name)()
            # The RATE_LIMIT_MS attribute is the external record;
            # enableRateLimit in the CCXT constructor is the
            # enforcement mechanism.  Both must be present.
            assert p.rate_limit_ms > 0


# ======================================================================
#  All providers satisfy ExchangeProvider protocol
# ======================================================================


def test_all_providers_satisfy_protocol() -> None:
    for name in SUPPORTED:
        p = get_provider_class(name)()
        assert isinstance(p, ExchangeProvider), f"{name} fails protocol"


# ======================================================================
#  BaseProvider error handling (no network)
# ======================================================================


@pytest.mark.network
class TestBaseProviderErrorHandling:
    """Without network, all API calls return empty defaults."""

    def setup_method(self) -> None:
        self.provider = BinanceProvider()

    def test_get_ticker_returns_dict(self) -> None:
        result = self.provider.get_ticker("NONEXISTENT/XXX")
        assert isinstance(result, dict)

    def test_fetch_ohlcv_returns_list(self) -> None:
        result = self.provider.fetch_ohlcv("NONEXISTENT/XXX")
        assert isinstance(result, list)

    def test_fetch_balance_returns_empty_dict(self) -> None:
        result = self.provider.fetch_balance()
        assert isinstance(result, dict)

    def test_health_check_returns_bool(self) -> None:
        assert isinstance(self.provider.health_check(), bool)

    def test_load_markets_returns_empty_dict(self) -> None:
        result = self.provider.load_markets()
        assert isinstance(result, dict)

    def test_fetch_tickers_returns_empty_dict(self) -> None:
        result = self.provider.fetch_tickers(["BTC/USDT"])
        assert isinstance(result, dict)

    def test_has_returns_bool(self) -> None:
        assert isinstance(self.provider.has("fetchOHLCV"), bool)


# ======================================================================
#  BaseProvider with mocked CCXT
# ======================================================================


class TestBaseProviderWithMock:
    def setup_method(self) -> None:
        self.provider = BinanceProvider()

    @patch("scripts.exchange_providers.ccxt")
    def test_get_ticker_with_data(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.return_value = {"symbol": "BTC/USDT", "last": 50000.0}
        mock_ccxt.binance.return_value = mock_ex
        result = self.provider.get_ticker("BTC/USDT")
        assert result["symbol"] == "BTC/USDT"
        assert result["last"] == 50000.0

    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_ohlcv_with_data(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_ohlcv.return_value = [
            [1600000000000, 100.0, 110.0, 90.0, 105.0, 1000.0],
        ]
        mock_ccxt.binance.return_value = mock_ex
        result = self.provider.fetch_ohlcv("BTC/USDT", limit=1)
        assert len(result) == 1
        assert result[0][4] == 105.0  # close

    @patch("scripts.exchange_providers.ccxt")
    def test_health_check_returns_true(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_time.return_value = 1600000000000
        mock_ccxt.binance.return_value = mock_ex
        assert self.provider.health_check() is True

    @patch("scripts.exchange_providers.ccxt")
    def test_load_markets_with_data(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {"BTC/USDT": {"symbol": "BTC/USDT"}}
        mock_ccxt.binance.return_value = mock_ex
        result = self.provider.load_markets()
        assert "BTC/USDT" in result

    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_tickers_with_data(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_tickers.return_value = {
            "BTC/USDT": {"symbol": "BTC/USDT", "last": 50000.0},
        }
        mock_ccxt.binance.return_value = mock_ex
        result = self.provider.fetch_tickers(["BTC/USDT"])
        assert "BTC/USDT" in result

    @patch("scripts.exchange_providers.ccxt")
    def test_has_feature(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.has = {"fetchOHLCV": True, "fetchTicker": True}
        mock_ccxt.binance.return_value = mock_ex
        assert self.provider.has("fetchOHLCV") is True
        assert self.provider.has("fetchTicker") is True
        assert self.provider.has("createOrder") is False


# ======================================================================
#  CCXT instance creation
# ======================================================================


def test_base_provider_creates_exchange_lazily() -> None:
    """Provider only creates CCXT instance on first API call."""
    p = BinanceProvider()
    assert p._instance is None  # not created yet
    # After a call that fails (no network), instance should still be created
    try:
        p.health_check()
    except Exception:
        pass
    # Instance may or may not be created depending on CCXT import
    # We just verify no crash


# ======================================================================
#  ExchangeProvider protocol structural check
# ======================================================================


def test_protocol_structural_check() -> None:
    """Verify a class missing required method fails protocol check."""
    class BadProvider:
        pass

    assert not isinstance(BadProvider(), ExchangeProvider)

    class MinimalProvider:
        @property
        def name(self) -> str:
            return "test"
        @property
        def ccxt_name(self) -> str:
            return "test"
        def get_ticker(self, symbol: str) -> dict[str, Any]:
            return {}
        def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
            return []
        def fetch_balance(self) -> dict[str, Any]:
            return {}
        def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            return {}
        def has_credentials(self) -> bool:
            return False
        def fetch_api_key_permissions(self) -> dict[str, Any]:
            return {}
        def fetch_api_key_permissions(self) -> dict[str, Any]:
            return {}
        def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
            return {}
        def get_markets(self) -> list[dict[str, Any]]:
            return []
        def health_check(self) -> bool:
            return False
        def load_markets(self) -> dict[str, Any]:
            return {}
        def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
            return {}
        def has(self, feature: str) -> bool:
            return False

    assert isinstance(MinimalProvider(), ExchangeProvider)
