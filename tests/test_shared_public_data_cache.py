"""Regression tests for BUG-5: shared public exchange client + TTL cache.

Every component (monitor, pipeline reconciliation, paper provider,
health check) used to build its own ccxt client and load markets
independently — a burst of concurrent clients tripped the indodax rate
limit (429) and every ticker fetch failed. These tests pin the shared
client identity and the TTL ticker cache behaviour of ``bot.data``.
"""

from typing import Any

import pytest

import bot.data


class _FakeExchange:
    """Fake ccxt-style exchange counting fetch_tickers calls."""

    def __init__(self) -> None:
        self.ticker_calls = 0

    def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
        self.ticker_calls += 1
        return {s: {"last": 1.0, "symbol": s} for s in symbols}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """The module caches are process-global — always clear them."""
    bot.data.clear_public_data_cache()
    yield
    bot.data.clear_public_data_cache()


class TestSharedPublicClient:
    def test_same_exchange_instance_is_shared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeExchange()
        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: fake)
        a = bot.data.get_cached_public_exchange("indodax")
        b = bot.data.get_cached_public_exchange("indodax")
        assert a is b
        assert a is fake

    def test_distinct_exchanges_keep_distinct_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fresh fake per build so the two cached clients differ.
        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: _FakeExchange())
        a = bot.data.get_cached_public_exchange("indodax")
        b = bot.data.get_cached_public_exchange("binance")
        assert a is not b


class TestTickerTtlCache:
    def test_repeat_fetch_within_ttl_hits_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeExchange()
        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: fake)
        first = bot.data.fetch_tickers_cached("indodax", ["GOAT/IDR"], ttl=3600.0)
        second = bot.data.fetch_tickers_cached("indodax", ["GOAT/IDR"], ttl=3600.0)
        assert first == {"GOAT/IDR": {"last": 1.0, "symbol": "GOAT/IDR"}}
        assert second == first
        assert fake.ticker_calls == 1

    def test_expired_ttl_refetches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeExchange()
        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: fake)
        bot.data.fetch_tickers_cached("indodax", ["GOAT/IDR"], ttl=3600.0)
        # ttl=0 forces every entry to be stale -> one new network call.
        bot.data.fetch_tickers_cached("indodax", ["GOAT/IDR"], ttl=0.0)
        assert fake.ticker_calls == 2

    def test_fetch_ticker_cached_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeExchange()
        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: fake)
        ticker = bot.data.fetch_ticker_cached("indodax", "GOAT/IDR", ttl=3600.0)
        assert ticker == {"last": 1.0, "symbol": "GOAT/IDR"}
        assert fake.ticker_calls == 1

    def test_network_failure_returns_what_cache_has(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Flaky:
            def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
                raise RuntimeError("rate limited")

        monkeypatch.setattr(bot.data, "build_public_exchange", lambda name: _Flaky())
        # Must degrade gracefully (empty result), never raise.
        assert bot.data.fetch_tickers_cached("indodax", ["GOAT/IDR"]) == {}
