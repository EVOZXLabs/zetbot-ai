"""Unit tests for scripts.scanner — multi-exchange scanning (Fase 0).

Covers the two hardcodes Fase 0 removes from MarketScanner:
  1. EXCHANGE_NAME was always "binance" regardless of the EXCHANGE env
     var / AppConfig.exchange.
  2. fetch_markets() always filtered for quote == "USDT" regardless of
     AppConfig.quote_currency (which blocks IDR-quoted exchanges like
     Indodax from ever returning a single pair).

No real network access is used — MarketData and ccxt are mocked.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.scanner import MarketScanner, PairAnalyzer, PairRaw


def _fake_config(exchange: str = "", quote_currency: str = "") -> SimpleNamespace:
    """Minimal stand-in for AppConfig — MarketScanner only reads
    `.exchange` and `.quote_currency` off it via getattr."""
    return SimpleNamespace(exchange=exchange, quote_currency=quote_currency)


# ======================================================================
#  MarketScanner — exchange & quote resolution
# ======================================================================


class TestMarketScannerExchangeResolution:
    @patch("scripts.scanner.MarketData")
    def test_resolves_exchange_from_config(self, mock_md: Any) -> None:
        scanner = MarketScanner(config=_fake_config(exchange="okx"))
        assert scanner.exchange_name == "okx"
        mock_md.assert_called_once_with(exchange_name="okx")

    @patch("scripts.scanner.MarketData")
    def test_resolves_each_supported_exchange(self, mock_md: Any) -> None:
        for name in ["binance", "bybit", "tokocrypto", "okx", "gate",
                     "kucoin", "mexc", "indodax"]:
            scanner = MarketScanner(config=_fake_config(exchange=name))
            assert scanner.exchange_name == name

    @patch("scripts.scanner.MarketData")
    def test_raises_when_config_has_no_exchange(
        self, mock_md: Any,
    ) -> None:
        import pytest
        with pytest.raises(ValueError, match="no exchange configured"):
            MarketScanner(config=_fake_config(exchange=""))

    @patch("scripts.scanner.MarketData")
    def test_exchange_name_is_lowercased(self, mock_md: Any) -> None:
        scanner = MarketScanner(config=_fake_config(exchange="OKX"))
        assert scanner.exchange_name == "okx"

    @patch("scripts.scanner.MarketData")
    def test_quote_currency_from_config(self, mock_md: Any) -> None:
        scanner = MarketScanner(config=_fake_config(exchange="indodax", quote_currency="IDR"))
        assert scanner.quote_currency == "IDR"

    @patch("scripts.scanner.MarketData")
    def test_quote_currency_defaults_to_usdt(self, mock_md: Any) -> None:
        scanner = MarketScanner(config=_fake_config(exchange="binance"))
        assert scanner.quote_currency == "USDT"


# ======================================================================
#  fetch_markets() — must filter by the CONFIGURED quote currency,
#  not a hardcoded "USDT"
# ======================================================================


class TestFetchMarketsQuoteFilter:
    @patch("scripts.scanner.MarketData")
    def test_filters_by_configured_quote_currency(self, mock_md: Any) -> None:
        raw_markets = [
            {"spot": True, "quote": "USDT", "active": True, "base": "BTC", "symbol": "BTC/USDT"},
            {"spot": True, "quote": "IDR", "active": True, "base": "BTC", "symbol": "BTC/IDR"},
            {"spot": True, "quote": "IDR", "active": True, "base": "ETH", "symbol": "ETH/IDR"},
        ]
        mock_md.return_value.exchange.fetch_markets.return_value = raw_markets

        scanner = MarketScanner(config=_fake_config(exchange="indodax", quote_currency="IDR"))
        pairs = scanner.fetch_markets()

        assert {p.symbol for p in pairs} == {"BTC/IDR", "ETH/IDR"}

    @patch("scripts.scanner.MarketData")
    def test_default_quote_currency_still_filters_usdt(self, mock_md: Any) -> None:
        raw_markets = [
            {"spot": True, "quote": "USDT", "active": True, "base": "BTC", "symbol": "BTC/USDT"},
            {"spot": True, "quote": "IDR", "active": True, "base": "BTC", "symbol": "BTC/IDR"},
        ]
        mock_md.return_value.exchange.fetch_markets.return_value = raw_markets

        scanner = MarketScanner(config=_fake_config(exchange="binance"))
        pairs = scanner.fetch_markets()

        assert [p.symbol for p in pairs] == ["BTC/USDT"]

    @patch("scripts.scanner.MarketData")
    def test_indodax_quote_no_longer_returns_zero_pairs(self, mock_md: Any) -> None:
        """Regression guard: before Fase 0, quote filtering was hardcoded
        to USDT, so an IDR-quoted exchange like Indodax always returned
        zero pairs no matter what QUOTE_CURRENCY was set to."""
        raw_markets = [
            {"spot": True, "quote": "IDR", "active": True, "base": "BTC", "symbol": "BTC/IDR"},
        ]
        mock_md.return_value.exchange.fetch_markets.return_value = raw_markets

        scanner = MarketScanner(config=_fake_config(exchange="indodax", quote_currency="IDR"))
        pairs = scanner.fetch_markets()

        assert len(pairs) == 1


# ======================================================================
#  PairAnalyzer — per-exchange ccxt resolution (thread-local cache)
# ======================================================================


class TestPairAnalyzerExchangeResolution:
    def setup_method(self) -> None:
        # Reset the thread-local cache between tests.
        PairAnalyzer._thread_local = None

    def test_get_exchange_uses_provider_registry(self) -> None:
        fake_ccxt = MagicMock()
        fake_okx_instance = MagicMock()
        fake_ccxt.okx.return_value = fake_okx_instance

        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            ex = PairAnalyzer._get_exchange("okx")

        assert ex is fake_okx_instance
        fake_ccxt.okx.assert_called_once()

    def test_get_exchange_resolves_tokocrypto_alias_to_binance_class(self) -> None:
        """Tokocrypto has no ccxt module of its own — it rides on
        ccxt.binance, same as scripts.exchange_providers.TokocryptoProvider."""
        fake_ccxt = MagicMock()
        fake_binance_instance = MagicMock()
        fake_ccxt.binance.return_value = fake_binance_instance

        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            ex = PairAnalyzer._get_exchange("tokocrypto")

        assert ex is fake_binance_instance

    def test_get_exchange_caches_per_exchange_name(self) -> None:
        fake_ccxt = MagicMock()
        fake_ccxt.okx.return_value = MagicMock()
        fake_ccxt.binance.return_value = MagicMock()

        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            ex1 = PairAnalyzer._get_exchange("okx")
            ex2 = PairAnalyzer._get_exchange("okx")
            ex3 = PairAnalyzer._get_exchange("binance")

        assert ex1 is ex2               # same exchange -> cached instance
        assert fake_ccxt.okx.call_count == 1
        assert ex3 is not ex1           # different exchange -> different instance

    def test_analyze_reports_exchange_in_error(self) -> None:
        """A failed fetch should say which exchange failed — needed to
        tell exchange-specific rate-limit/auth errors apart when several
        exchanges are configured across environments."""
        fake_ccxt = MagicMock()
        fake_instance = MagicMock()
        fake_instance.fetch_ohlcv.side_effect = Exception("rate limited")
        fake_ccxt.gate.return_value = fake_instance

        pair = PairRaw(symbol="BTC/USDT", base="BTC")
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            result = PairAnalyzer.analyze(pair, exchange_name="gate")

        assert result.status == "error"
        assert "gate" in result.error
