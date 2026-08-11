"""Regression tests: exchange fetch calls must be retry-wrapped.

Previously ``MarketScanner`` called ``self.md.exchange.fetch_markets()``
and ``self.md.exchange.fetch_tickers()`` directly on the raw ccxt
client, which has NO retry of its own. The first pipeline after a cold
start (DNS/TLS/connect to e.g. Indodax ``/api/pairs``) would therefore
fail the whole scan and only succeed on the second run once the
connection was warm. The realtime commands (``/detail``, ``/signals``,
``/market``) had the same direct-call leak.

The fix routes every one of those calls through
``scripts.exchange_providers.exchange_call_with_retry`` — which retries
on transient ``ccxt.NetworkError`` / ``RequestTimeout`` /
``ExchangeNotAvailable`` with exponential backoff. Because the retry is
keyed off ``self.CCXT_NAME`` / ``self.exchange_name`` (never a hardcoded
exchange string), it protects Binance, Tokocrypto, OKX, Indodax, … alike.

These tests prove:
  * a transient failure on the FIRST call is retried and the scan still
    succeeds (no more "only pipeline #1 fails"),
  * ``MarketData.fetch_markets`` / ``fetch_tickers`` and
    ``RealtimeMarketData`` all go through the shared retry helper,
  * the fix is exchange-agnostic (exercised for indodax AND binance).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import ccxt
import pytest

from bot.data import MarketData
from scripts.scanner import MarketScanner


def _fake_config(exchange: str = "indodax", quote_currency: str = "IDR") -> SimpleNamespace:
    return SimpleNamespace(exchange=exchange, quote_currency=quote_currency)


class _FlakyExchange:
    """Raw ccxt stand-in that fails the first ``failures`` calls of a given
    method with a transient NetworkError, then succeeds — simulating a
    cold-start. ``failures`` is per-method so a test can flake a single
    endpoint (e.g. ``fetch_ticker``) without disturbing the others."""

    def __init__(self, failures: int = 0, payload: Any = None,
                 payloads: dict[str, Any] | None = None) -> None:
        self._failures = failures
        self._payload = payload
        self._payloads = payloads or {}
        self.calls: dict[str, int] = {}

    def _attempt(self, method: str) -> Any:
        self.calls[method] = self.calls.get(method, 0) + 1
        if self.calls[method] <= self._failures:
            raise ccxt.NetworkError(f"cold start {method} #{self.calls[method]}")
        return self._payloads.get(method, self._payload)

    def fetch_markets(self) -> Any:
        return self._attempt("fetch_markets")

    def fetch_tickers(self, symbols=None) -> Any:
        return self._attempt("fetch_tickers")

    def fetch_ticker(self, symbol: str) -> Any:
        return self._attempt("fetch_ticker")

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=200) -> Any:
        return self._attempt("fetch_ohlcv")


@pytest.fixture
def _patch_exchange():
    """Inject a controllable fake exchange into MarketData without touching
    the real network. ``new_exchange`` is set by each test."""
    holder: dict[str, Any] = {}

    def _factory(exchange_name: str) -> Any:
        return holder["new_exchange"]

    with patch("bot.data.get_cached_public_exchange", side_effect=_factory):
        yield holder


# ---------------------------------------------------------------------------
#  MarketData — the shared fetch layer used by the scanner
# ---------------------------------------------------------------------------


class TestMarketDataFetchRetry:
    def test_fetch_markets_retries_transient_cold_start(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        markets = [
            {"symbol": "BICO/IDR", "base": "BICO", "quote": "IDR",
             "spot": True, "active": True},
            {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
             "spot": True, "active": True},
        ]
        flaky = _FlakyExchange(failures=2, payloads={"fetch_markets": markets})
        _patch_exchange["new_exchange"] = flaky

        md = MarketData(exchange_name="indodax", exchange=flaky)
        result = md.fetch_markets()

        # Two transient failures + one success == 3 calls, no raise.
        assert flaky.calls["fetch_markets"] == 3
        assert result == markets

    def test_fetch_markets_works_on_first_try(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        markets = [{"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
                    "spot": True, "active": True}]
        flaky = _FlakyExchange(failures=0, payloads={"fetch_markets": markets})
        _patch_exchange["new_exchange"] = flaky

        md = MarketData(exchange_name="binance", exchange=flaky)
        assert md.fetch_markets() == markets
        assert flaky.calls["fetch_markets"] == 1

    def test_fetch_tickers_retries_transient_cold_start(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        tickers = {"BICO/IDR": {"last": 891.0, "quoteVolume": 1_000_000.0}}
        # markets starts empty so fetch_tickers must pre-load it first
        # (then the tickers call itself is transient-flaky).
        flaky = _FlakyExchange(
            failures=1,
            payloads={"fetch_tickers": tickers, "fetch_markets": [{"symbol": "BICO/IDR"}]},
        )
        _patch_exchange["new_exchange"] = flaky

        md = MarketData(exchange_name="indodax", exchange=flaky)
        assert md.fetch_tickers() == tickers
        # 1 fetch_markets (pre-load) + 2 fetch_tickers (1 fail + 1 success)
        assert flaky.calls["fetch_tickers"] == 2

    def test_fetch_tickers_preloads_markets_to_avoid_none_concat(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        """Regression for the cold-start ``TypeError: can only concatenate
        str (not "NoneType") to str`` from ccxt's fetch_tickers (its
        internal load_markets hits a None market map). fetch_tickers must
        pre-load the market map via fetch_markets so the call never
        reaches that crash."""
        tickers = {"BICO/IDR": {"last": 891.0, "quoteVolume": 1_000_000.0}}
        flaky = _FlakyExchange(
            failures=0,
            payloads={"fetch_tickers": tickers, "fetch_markets": [{"symbol": "BICO/IDR"}]},
        )
        _patch_exchange["new_exchange"] = flaky

        md = MarketData(exchange_name="indodax", exchange=flaky)
        # With markets NOT pre-loaded, this used to raise TypeError deep
        # in ccxt. It must now succeed (pre-loads markets transparently).
        assert md.fetch_tickers() == tickers
        assert flaky.calls.get("fetch_markets", 0) >= 1

    def test_fetch_markets_passes_through_exchange_name(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        """The retry label must carry the configured exchange (never a
        hardcoded string) so the fix is exchange-agnostic."""
        markets = [{"symbol": "ETH/USDT", "base": "ETH", "quote": "USDT",
                    "spot": True, "active": True}]
        flaky = _FlakyExchange(failures=0, payloads={"fetch_markets": markets})
        _patch_exchange["new_exchange"] = flaky

        md = MarketData(exchange_name="tokocrypto", exchange=flaky)
        # No assertion on logging here, but it must not raise and must
        # honor the exchange name end-to-end.
        assert md.fetch_markets() == markets
        assert md.exchange_name == "tokocrypto"


# ---------------------------------------------------------------------------
#  MarketScanner — runs end-to-end against the retry-wrapped layer
# ---------------------------------------------------------------------------


class TestMarketScannerFetchRetry:
    def test_fetch_markets_survives_cold_start(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        raw = [
            {"symbol": "BICO/IDR", "base": "BICO", "quote": "IDR",
             "spot": True, "active": True},
            {"symbol": "DODO/IDR", "base": "DODO", "quote": "IDR",
             "spot": True, "active": True},
            {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
             "spot": True, "active": True},
        ]
        flaky = _FlakyExchange(failures=2, payloads={"fetch_markets": raw})
        _patch_exchange["new_exchange"] = flaky

        scanner = MarketScanner(config=_fake_config(
            exchange="indodax", quote_currency="IDR"))
        pairs = scanner.fetch_markets()

        # Cold start failed twice; retry layer must still deliver pairs.
        assert flaky.calls["fetch_markets"] == 3
        assert {p.symbol for p in pairs} == {"BICO/IDR", "DODO/IDR"}

    def test_attach_tickers_survives_cold_start(
        self, _patch_exchange: dict[str, Any],
    ) -> None:
        tickers = {
            "BICO/IDR": {"last": 891.0, "quoteVolume": 1_000_000.0,
                         "percentage": 2.5, "high": 900.0, "low": 880.0},
        }
        flaky = _FlakyExchange(failures=2, payloads={"fetch_tickers": tickers})
        _patch_exchange["new_exchange"] = flaky

        scanner = MarketScanner(config=_fake_config(
            exchange="indodax", quote_currency="IDR"))
        pair = MagicMock()
        pair.symbol = "BICO/IDR"
        scanner.attach_tickers([pair])

        assert flaky.calls["fetch_tickers"] == 3
        assert pair.price == 891.0
        assert pair.volume_24h == 1_000_000.0

    def test_works_for_binance_too(self, _patch_exchange: dict[str, Any]) -> None:
        """The retry is exchange-agnostic — prove it for a non-Indodax
        exchange as well."""
        raw = [
            {"symbol": "BICO/USDT", "base": "BICO", "quote": "USDT",
             "spot": True, "active": True},
            {"symbol": "BTC/IDR", "base": "BTC", "quote": "IDR",
             "spot": True, "active": True},
        ]
        flaky = _FlakyExchange(failures=1, payloads={"fetch_markets": raw})
        _patch_exchange["new_exchange"] = flaky

        scanner = MarketScanner(config=_fake_config(
            exchange="binance", quote_currency="USDT"))
        pairs = scanner.fetch_markets()
        assert flaky.calls["fetch_markets"] == 2  # 1 fail + 1 success
        assert {p.symbol for p in pairs} == {"BICO/USDT"}


# ---------------------------------------------------------------------------
#  Realtime commands — same direct-call leak, same fix
# ---------------------------------------------------------------------------


class _FakeManager:
    def __init__(self, name: str, quote: str) -> None:
        self.name = name
        self.quote_currency = quote

    def get_provider(self) -> Any:
        return self


class TestRealtimeFetchRetry:
    def test_fetch_ticker_retries_cold_start(self) -> None:
        from scripts.realtime_market import RealtimeMarketData

        tickers = {"BICO/IDR": {"last": 891.0, "quoteVolume": 1_000_000.0}}
        flaky = _FlakyExchange(failures=2, payloads={"fetch_ticker": tickers["BICO/IDR"]})
        mgr = _FakeManager("indodax", "IDR")

        rt = RealtimeMarketData(mgr, _public_exchange_factory=lambda n: flaky)
        result = rt.fetch_ticker("BICO/IDR")

        assert flaky.calls["fetch_ticker"] == 3
        assert result == tickers["BICO/IDR"]

    def test_top_candidates_retries_cold_start(self) -> None:
        from scripts.realtime_market import RealtimeMarketData

        tickers = {
            "BICO/IDR": {"last": 891.0, "quoteVolume": 1_000_000.0},
            "DODO/IDR": {"last": 100.0, "quoteVolume": 500_000.0},
        }
        flaky = _FlakyExchange(failures=1, payloads={"fetch_tickers": tickers})
        mgr = _FakeManager("indodax", "IDR")

        rt = RealtimeMarketData(mgr, _public_exchange_factory=lambda n: flaky)
        # First call populates the TTL cache; a second (forcing expiry) would
        # re-fetch. Just assert the cold-start fetch itself retried.
        rt._tickers_list_cache = (0.0, {})
        rt.top_candidates(limit=10)

        assert flaky.calls["fetch_tickers"] == 2  # 1 fail + 1 success
