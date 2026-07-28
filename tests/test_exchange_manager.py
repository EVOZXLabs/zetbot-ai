"""Unit tests for ExchangeManager."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.exchange_manager import ExchangeManager


class TestExchangeManagerInit:
    def test_default_active_is_binance(self) -> None:
        mgr = ExchangeManager()
        assert mgr.name == "binance"

    def test_custom_active(self) -> None:
        mgr = ExchangeManager(active="bybit")
        assert mgr.name == "bybit"

    def test_list_providers(self) -> None:
        mgr = ExchangeManager()
        providers = mgr.list_providers()
        assert "binance" in providers
        assert "bybit" in providers
        assert "tokocrypto" in providers
        assert "okx" in providers
        assert "gate" in providers
        assert "kucoin" in providers
        assert "mexc" in providers


class TestExchangeManagerSetActive:
    def test_set_active_valid(self) -> None:
        mgr = ExchangeManager(active="binance")
        mgr.set_active("bybit")
        assert mgr.name == "bybit"

    def test_set_active_case_insensitive(self) -> None:
        mgr = ExchangeManager(active="binance")
        mgr.set_active("BYBIT")
        assert mgr.name == "bybit"

    def test_set_active_unknown_raises(self) -> None:
        mgr = ExchangeManager()
        try:
            mgr.set_active("unknown_exchange")
            assert False, "Expected KeyError"
        except KeyError:
            pass

    def test_set_active_then_provider_returns_new(self) -> None:
        mgr = ExchangeManager(active="binance")
        mgr.set_active("bybit")
        provider = mgr.get_provider()
        assert provider.name == "bybit"


class TestExchangeManagerGetProvider:
    def test_get_provider_returns_active_by_default(self) -> None:
        mgr = ExchangeManager(active="okx")
        provider = mgr.get_provider()
        assert provider.name == "okx"

    def test_get_provider_named(self) -> None:
        mgr = ExchangeManager(active="binance")
        provider = mgr.get_provider("gate")
        assert provider.name == "gate"
        # Active should remain unchanged
        assert mgr.name == "binance"

    def test_provider_is_singleton(self) -> None:
        mgr = ExchangeManager()
        p1 = mgr.get_provider("bybit")
        p2 = mgr.get_provider("bybit")
        assert p1 is p2  # same instance (lazy cached)


@pytest.mark.network
class TestExchangeManager:
    def setup_method(self) -> None:
        self.mgr = ExchangeManager(active="binance")

    @pytest.mark.network
    def test_all_interface_methods_delegate(self) -> None:
        """All IExchangeManager methods should work without network (return defaults)."""
        assert isinstance(self.mgr.get_ticker("XXX/YYY"), dict)
        assert isinstance(self.mgr.fetch_ohlcv("XXX/YYY"), list)
        assert isinstance(self.mgr.fetch_balance(), dict)
        assert isinstance(self.mgr.get_markets(), list)
        assert isinstance(self.mgr.health_check(), bool)
        assert isinstance(self.mgr.load_markets(), dict)
        assert isinstance(self.mgr.fetch_tickers(), dict)
        assert isinstance(self.mgr.has("fetchOHLCV"), bool)

    @pytest.mark.network
    def test_list_connected(self) -> None:
        result = self.mgr.list_connected()
        assert isinstance(result, dict)
        for exchange in self.mgr.list_providers():
            assert exchange in result

    @pytest.mark.network
    def test_health_check_via_delegate(self) -> None:
        # Should return a bool (True or False depending on network)
        result = self.mgr.health_check()
        assert isinstance(result, bool)


class TestExchangeManagerWithMocks:
    @patch("scripts.exchange_providers.ccxt")
    def test_get_ticker_delegates_to_active(self, mock_ccxt: Any) -> None:
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.return_value = {"symbol": "BTC/USDT", "last": 50000.0}
        mock_ccxt.binance.return_value = mock_ex

        mgr = ExchangeManager(active="binance")
        result = mgr.get_ticker("BTC/USDT")
        assert result["last"] == 50000.0
        mock_ex.fetch_ticker.assert_called_once_with("BTC/USDT")

    @patch("scripts.exchange_providers.ccxt")
    def test_active_switch_changes_delegate(self, mock_ccxt: Any) -> None:
        mock_binance = MagicMock()
        mock_binance.fetch_ticker.return_value = {"exchange": "binance"}
        mock_bybit = MagicMock()
        mock_bybit.fetch_ticker.return_value = {"exchange": "bybit"}
        mock_ccxt.binance.return_value = mock_binance
        mock_ccxt.bybit.return_value = mock_bybit

        mgr = ExchangeManager(active="binance")
        assert mgr.get_ticker("X")["exchange"] == "binance"

        mgr.set_active("bybit")
        assert mgr.get_ticker("X")["exchange"] == "bybit"

        # Original should still be cached
        assert mgr.get_provider("binance") is not mgr.get_provider("bybit")


class TestExchangeManagerListConnected:
    @patch("scripts.exchange_providers.ccxt")
    def test_list_connected_with_mocks(self, mock_ccxt: Any) -> None:
        mock_binance = MagicMock()
        mock_binance.fetch_time.return_value = 1000
        mock_bybit = MagicMock()
        mock_bybit.fetch_time.return_value = 2000

        def make_mock(name: str) -> MagicMock:
            m = MagicMock()
            m.fetch_time.return_value = 1000
            return m

        def side_effect(**kwargs: Any) -> MagicMock:
            # Ignore kwargs (like enableRateLimit, etc.)
            return make_mock("generic")

        # Make all exchanges work
        for attr in ["binance", "bybit", "okx", "gate", "kucoin", "mexc"]:
            setattr(mock_ccxt, attr, MagicMock(return_value=make_mock(attr)))
        # tokocrypto uses binance class
        mock_ccxt.binance.return_value = make_mock("binance")

        mgr = ExchangeManager()
        result = mgr.list_connected()
        assert isinstance(result, dict)
        for name in mgr.list_providers():
            assert name in result
