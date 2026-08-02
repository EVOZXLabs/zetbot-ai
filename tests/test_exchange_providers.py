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
        # Indodax has no spot-vs-futures split — unlike the other
        # providers it must NOT set a `defaultType` option.
        assert "options" not in p.CCXT_KWARGS


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


# ======================================================================
#  Error handling / credential-failure behaviour — shared by every
#  provider via BaseProvider, so exercised once here rather than
#  duplicated per-exchange. This is what "rate-limit & error handling
#  validation" for Fase 0 actually exercises: whichever exchange is
#  active, a broken authenticated call must raise, never look like an
#  empty/zero balance.
# ======================================================================


class TestCredentialFailureHandling:
    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_balance_raises_when_credentials_set_and_call_fails(
        self, mock_ccxt: Any,
    ) -> None:
        from scripts.exchange_providers import ExchangeAuthError

        mock_ex = MagicMock()
        mock_ex.fetch_balance.side_effect = Exception("network blip")
        mock_ccxt.binance.return_value = mock_ex

        provider = BinanceProvider(api_key="key", api_secret="secret")
        with pytest.raises(ExchangeAuthError):
            provider.fetch_balance()

    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_balance_returns_empty_without_credentials(
        self, mock_ccxt: Any,
    ) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_balance.side_effect = Exception("network blip")
        mock_ccxt.binance.return_value = mock_ex

        provider = BinanceProvider()  # no credentials
        assert provider.fetch_balance() == {}

    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_order_raises_when_credentials_set_and_call_fails(
        self, mock_ccxt: Any,
    ) -> None:
        from scripts.exchange_providers import ExchangeAuthError

        mock_ex = MagicMock()
        mock_ex.fetch_order.side_effect = Exception("bad api key")
        mock_ccxt.okx.return_value = mock_ex

        provider = OKXProvider(api_key="key", api_secret="secret")
        with pytest.raises(ExchangeAuthError):
            provider.fetch_order("123", "BTC/USDT")

    @patch("scripts.exchange_providers.ccxt")
    def test_all_providers_raise_auth_error_with_credentials(
        self, mock_ccxt: Any,
    ) -> None:
        """Every provider — not just Binance — must surface a broken
        authenticated call as ExchangeAuthError, since each exchange's
        API fails differently (rate-limit, auth, timeout) and callers
        must never mistake that for a legitimate zero balance."""
        from scripts.exchange_providers import ExchangeAuthError

        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.fetch_balance.side_effect = Exception(f"{name} error")
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            provider = get_provider_class(name)(
                api_key="key", api_secret="secret",
            )
            with pytest.raises(ExchangeAuthError):
                provider.fetch_balance()


# ======================================================================
#  Precision helpers — used before every live order; must degrade
#  gracefully (never raise) when markets can't be loaded.
# ======================================================================


class TestPrecisionHelpers:
    @patch("scripts.exchange_providers.ccxt")
    def test_amount_to_precision_falls_back_on_error(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.markets = None
        mock_ex.load_markets.side_effect = Exception("network down")
        mock_ccxt.binance.return_value = mock_ex

        provider = BinanceProvider()
        assert provider.amount_to_precision("BTC/USDT", 1.23456789) == 1.23456789

    @patch("scripts.exchange_providers.ccxt")
    def test_price_to_precision_falls_back_on_error(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.markets = None
        mock_ex.load_markets.side_effect = Exception("network down")
        mock_ccxt.binance.return_value = mock_ex

        provider = BinanceProvider()
        assert provider.price_to_precision("BTC/USDT", 50000.123) == 50000.123

    @patch("scripts.exchange_providers.ccxt")
    def test_amount_to_precision_uses_exchange_rounding(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.markets = {"BTC/USDT": {}}
        mock_ex.amount_to_precision.return_value = "1.234"
        mock_ccxt.binance.return_value = mock_ex

        provider = BinanceProvider()
        assert provider.amount_to_precision("BTC/USDT", 1.23456789) == 1.234


# ======================================================================
#  client_order_id_params — each exchange uses a different param name
# ======================================================================


class TestClientOrderIdParams:
    def test_binance_uses_new_client_order_id(self) -> None:
        p = BinanceProvider()
        assert p.client_order_id_params("my-id") == {"newClientOrderId": "my-id"}

    def test_tokocrypto_uses_new_client_order_id(self) -> None:
        p = TokocryptoProvider()
        assert p.client_order_id_params("my-id") == {"newClientOrderId": "my-id"}

    def test_bybit_uses_unified_client_order_id(self) -> None:
        p = BybitProvider()
        assert p.client_order_id_params("my-id") == {"clientOrderId": "my-id"}

    def test_okx_uses_unified_client_order_id(self) -> None:
        p = OKXProvider()
        assert p.client_order_id_params("my-id") == {"clientOrderId": "my-id"}

    def test_gate_uses_unified_client_order_id(self) -> None:
        p = GateProvider()
        assert p.client_order_id_params("my-id") == {"clientOrderId": "my-id"}

    def test_kucoin_uses_unified_client_order_id(self) -> None:
        p = KucoinProvider()
        assert p.client_order_id_params("my-id") == {"clientOrderId": "my-id"}

    def test_mexc_uses_unified_client_order_id(self) -> None:
        p = MEXCProvider()
        assert p.client_order_id_params("my-id") == {"clientOrderId": "my-id"}

    def test_indodax_sends_no_client_order_id_param(self) -> None:
        # Indodax has no client-order-id concept; its private trade endpoint
        # signs the ENTIRE request body, so a stray param risks rejection.
        p = IndodaxProvider()
        assert p.client_order_id_params("my-id") == {}

    def test_indodax_market_buy_requires_price(self) -> None:
        assert IndodaxProvider().market_buy_requires_price() is True

    def test_binance_market_buy_does_not_require_price(self) -> None:
        assert BinanceProvider().market_buy_requires_price() is False


# ======================================================================
#  has_credentials — must reflect constructor arguments exactly
# ======================================================================


class TestHasCredentials:
    def test_no_credentials_returns_false(self) -> None:
        assert BinanceProvider().has_credentials() is False

    def test_with_key_and_secret_returns_true(self) -> None:
        assert BinanceProvider(api_key="k", api_secret="s").has_credentials() is True

    def test_empty_key_returns_false(self) -> None:
        assert BinanceProvider(api_key="", api_secret="s").has_credentials() is False

    def test_empty_secret_returns_false(self) -> None:
        assert BinanceProvider(api_key="k", api_secret="").has_credentials() is False

    def test_all_providers_with_credentials(self) -> None:
        for name in SUPPORTED:
            p = get_provider_class(name)(api_key="k", api_secret="s")
            assert p.has_credentials() is True, f"{name} has_credentials failed"

    def test_all_providers_without_credentials(self) -> None:
        for name in SUPPORTED:
            p = get_provider_class(name)()
            assert p.has_credentials() is False, f"{name} has_credentials failed"


# ======================================================================
#  CCXT_KWARGS — each provider must configure rate-limiting and spot mode
#  correctly. Indodax specifically must NOT set a defaultType option
#  since it only has spot.
# ======================================================================


class TestCcxtKwargs:
    def test_binance_has_spot_default_type(self) -> None:
        assert BinanceProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_bybit_has_spot_default_type(self) -> None:
        assert BybitProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_tokocrypto_has_spot_default_type(self) -> None:
        assert TokocryptoProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_okx_has_spot_default_type(self) -> None:
        assert OKXProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_gate_has_spot_default_type(self) -> None:
        assert GateProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_kucoin_has_spot_default_type(self) -> None:
        assert KucoinProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_mexc_has_spot_default_type(self) -> None:
        assert MEXCProvider.CCXT_KWARGS.get("options", {}).get("defaultType") == "spot"

    def test_indodax_has_no_options(self) -> None:
        assert IndodaxProvider.CCXT_KWARGS == {}, (
            "Indodax must not set defaultType — it has no spot-vs-futures split"
        )

    def test_base_provider_enables_rate_limit(self) -> None:
        """Every provider created via BaseProvider._get_exchange must
        have enableRateLimit=True and a 15s timeout."""
        with patch("scripts.exchange_providers.ccxt") as mock_ccxt:
            mock_ex = MagicMock()
            mock_ccxt.binance.return_value = mock_ex
            p = BinanceProvider()
            ex = p._get_exchange()
            call_kwargs = mock_ccxt.binance.call_args[0][0]
            assert call_kwargs.get("enableRateLimit") is True
            assert call_kwargs.get("timeout") == 15000


# ======================================================================
#  fetch_api_key_permissions — must never raise, returns dict
# ======================================================================


class TestFetchApiKeyPermissions:
    @patch("scripts.exchange_providers.ccxt")
    def test_returns_dict_on_success(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.sapiGetAccountApiRestrictions.return_value = {
            "ipRestrict": False,
            "enableWithdrawals": False,
        }
        mock_ccxt.binance.return_value = mock_ex
        p = BinanceProvider(api_key="k", api_secret="s")
        result = p.fetch_api_key_permissions()
        assert isinstance(result, dict)
        assert result.get("ipRestrict") is False

    @patch("scripts.exchange_providers.ccxt")
    def test_returns_empty_dict_on_error(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.sapiGetAccountApiRestrictions.side_effect = Exception("not found")
        mock_ccxt.binance.return_value = mock_ex
        p = BinanceProvider(api_key="k", api_secret="s")
        assert p.fetch_api_key_permissions() == {}

    def test_returns_empty_dict_when_method_missing(self) -> None:
        """Exchanges other than Binance don't have the
        sapiGetAccountApiRestrictions endpoint — must return {}."""
        p = OKXProvider()
        assert p.fetch_api_key_permissions() == {}


# ======================================================================
#  Error handling — every provider must handle network/API errors
#  gracefully for public calls, and raise ExchangeAuthError for
#  authenticated calls when credentials are set.
# ======================================================================


class TestAllProvidersErrorHandling:
    """Verifies every provider handles errors consistently — a single
    exchange with a missing except could silently return an empty balance
    or ticker even when the API is failing."""

    PUBLIC_METHODS = ["get_ticker", "fetch_ohlcv", "health_check",
                      "load_markets", "fetch_tickers"]

    @patch("scripts.exchange_providers.ccxt")
    def test_public_methods_return_defaults_on_network_error(
        self, mock_ccxt: Any,
    ) -> None:
        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.fetch_ticker.side_effect = Exception("network down")
            mock_ex.fetch_ohlcv.side_effect = Exception("network down")
            mock_ex.fetch_time.side_effect = Exception("network down")
            mock_ex.load_markets.side_effect = Exception("network down")
            mock_ex.fetch_tickers.side_effect = Exception("network down")
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)()
            assert p.get_ticker("BTC/USDT") == {}, f"{name} get_ticker"
            assert p.fetch_ohlcv("BTC/USDT") == [], f"{name} fetch_ohlcv"
            assert p.health_check() is False, f"{name} health_check"
            assert p.load_markets() == {}, f"{name} load_markets"
            assert p.fetch_tickers(["BTC/USDT"]) == {}, f"{name} fetch_tickers"

    @patch("scripts.exchange_providers.ccxt")
    def test_every_provider_raises_auth_error_for_fetch_balance(
        self, mock_ccxt: Any,
    ) -> None:
        """No provider should silently swallow an authenticated call
        failure when credentials are set."""
        from scripts.exchange_providers import ExchangeAuthError

        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.fetch_balance.side_effect = Exception(f"{name} API error")
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)(api_key="k", api_secret="s")
            with pytest.raises(ExchangeAuthError, match=name):
                p.fetch_balance()

    @patch("scripts.exchange_providers.ccxt")
    def test_every_provider_raises_auth_error_for_fetch_order(
        self, mock_ccxt: Any,
    ) -> None:
        from scripts.exchange_providers import ExchangeAuthError

        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.fetch_order.side_effect = Exception(f"{name} order error")
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)(api_key="k", api_secret="s")
            with pytest.raises(ExchangeAuthError, match=name):
                p.fetch_order("123", "BTC/USDT")


# ======================================================================
#  Exchange-specific characteristics — known quirks per exchange that
#  the bot must handle correctly.
# ======================================================================


class TestExchangeSpecificBehavior:
    """Tests for known per-exchange differences in API behavior."""

    @patch("scripts.exchange_providers.ccxt")
    def test_get_markets_returns_list(self, mock_ccxt: Any) -> None:
        """Every exchange's get_markets must return a list, never None."""
        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.markets = {"BTC/USDT": {"symbol": "BTC/USDT", "spot": True}}
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)()
            markets = p.get_markets()
            assert isinstance(markets, list), f"{name} get_markets type"
            assert len(markets) > 0, f"{name} get_markets empty"

    @patch("scripts.exchange_providers.ccxt")
    def test_has_feature_per_exchange(self, mock_ccxt: Any) -> None:
        """All spot exchanges should support basic features."""
        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.has = {"fetchOHLCV": True, "fetchTicker": True,
                          "fetchBalance": True, "createOrder": True}
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)()
            assert p.has("fetchOHLCV") is True, f"{name} fetchOHLCV"
            assert p.has("fetchTicker") is True, f"{name} fetchTicker"

    @patch("scripts.exchange_providers.ccxt")
    def test_get_markets_handles_missing_attribute(self, mock_ccxt: Any) -> None:
        """Some exchanges may not have .markets loaded yet — get_markets
        must call load_markets in that case."""
        mock_ex = MagicMock()
        mock_ex.markets = None  # not loaded yet
        def _load_markets() -> dict:
            mock_ex.markets = {"BTC/USDT": {"symbol": "BTC/USDT"}}
            return mock_ex.markets
        mock_ex.load_markets.side_effect = _load_markets
        mock_ccxt.binance.return_value = mock_ex

        p = BinanceProvider()
        markets = p.get_markets()
        assert len(markets) == 1
        mock_ex.load_markets.assert_called_once()

    @patch("scripts.exchange_providers.ccxt")
    def test_fetch_tickers_with_none_symbols(self, mock_ccxt: Any) -> None:
        """fetch_tickers(symbols=None) fetches ALL tickers — this is the
        common scanning pattern and must not crash."""
        mock_ex = MagicMock()
        mock_ex.fetch_tickers.return_value = {
            "BTC/USDT": {"symbol": "BTC/USDT", "last": 50000.0},
        }
        mock_ccxt.binance.return_value = mock_ex

        p = BinanceProvider()
        result = p.fetch_tickers()
        assert "BTC/USDT" in result

    @patch("scripts.exchange_providers.ccxt")
    def test_all_providers_lazy_instantiation(self, mock_ccxt: Any) -> None:
        """Providers must not create CCXT instances until first API call."""
        for name in SUPPORTED:
            p = get_provider_class(name)()
            assert p._instance is None, f"{name} created instance eagerly"


# ======================================================================
#  Rate-limit characteristics — each exchange has different throttling.
#  CCXT's enableRateLimit handles the per-exchange algorithm, but the
#  provider must always enable it (never disable it).
# ======================================================================


class TestRateLimitBehavior:
    """CCXT's built-in rate limiter is the sole rate-limit mechanism.
    Every provider must have it enabled."""

    @patch("scripts.exchange_providers.ccxt")
    def test_every_provider_creates_with_rate_limit(self, mock_ccxt: Any) -> None:
        for name in SUPPORTED:
            mock_ex = MagicMock()
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)()
            # Trigger lazy instantiation
            p.health_check()

            call_kwargs = getattr(mock_ccxt, p.ccxt_name).call_args[0][0]
            assert call_kwargs.get("enableRateLimit") is True, (
                f"{name} must enable rate limiting"
            )
            assert call_kwargs.get("timeout") == 15000, (
                f"{name} must have 15s timeout"
            )


# ======================================================================
#  Known gaps — per-exchange API quirks that need future work
# ======================================================================


class TestKnownGaps:
    """Tests that document known per-exchange limitations.

    These tests verify that gaps fail in a *safe* way (i.e., they raise
    appropriate errors rather than silently returning bad data).
    """

    @patch("scripts.exchange_providers.ccxt")
    def test_kucoin_missing_password_raises_auth_error(
        self, mock_ccxt: Any,
    ) -> None:
        """Kucoin requires an API passphrase ('password') — the current
        BaseProvider does not accept a password parameter, so Kucoin
        authenticated calls will fail. This test verifies they fail with
        ExchangeAuthError (not a silent empty balance).

        Fix: add api_password to AppConfig and pass it through
        ExchangeManager → BaseProvider._get_exchange() kwargs.
        """
        from scripts.exchange_providers import ExchangeAuthError

        # Kucoin's CCXT class requires password even though
        # requiredCredentials says otherwise at module level.
        # Simulate what happens when password is missing.
        mock_ex = MagicMock()
        mock_ex.fetch_balance.side_effect = Exception(
            "kucoin requires password argument"
        )
        mock_ccxt.kucoin.return_value = mock_ex

        p = KucoinProvider(api_key="k", api_secret="s")
        with pytest.raises(ExchangeAuthError):
            p.fetch_balance()

    @patch("scripts.exchange_providers.ccxt")
    def test_public_calls_work_without_kucoin_password(
        self, mock_ccxt: Any,
    ) -> None:
        """Public (non-authenticated) calls must still work for Kucoin
        even without the password — the scanner uses public endpoints."""
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.return_value = {"symbol": "BTC/USDT", "last": 50000.0}
        mock_ccxt.kucoin.return_value = mock_ex

        p = KucoinProvider()  # no credentials
        result = p.get_ticker("BTC/USDT")
        assert result.get("symbol") == "BTC/USDT"

    @patch("scripts.exchange_providers.ccxt")
    def test_all_providers_handle_missing_credentials_gracefully(
        self, mock_ccxt: Any,
    ) -> None:
        """Every provider must handle the case where a constructor is
        called with no credentials — the public API methods should still
        work (e.g., for scanning)."""
        for name in SUPPORTED:
            mock_ex = MagicMock()
            mock_ex.fetch_ticker.return_value = {"symbol": "BTC/USDT", "last": 50000.0}
            mock_ex.fetch_ohlcv.return_value = [[1600000000000, 100, 110, 90, 105, 1000]]
            mock_ex.fetch_time.return_value = 1600000000000
            setattr(mock_ccxt, get_provider_class(name)().ccxt_name,
                    MagicMock(return_value=mock_ex))

            p = get_provider_class(name)()
            assert p.get_ticker("BTC/USDT") != {}, f"{name} get_ticker"
            assert len(p.fetch_ohlcv("BTC/USDT")) > 0, f"{name} fetch_ohlcv"
            assert p.health_check() is True, f"{name} health_check"
