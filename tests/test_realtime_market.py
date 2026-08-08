"""Regression tests: Telegram market commands must be REALTIME.

Proves:
1. /detail reads from the live exchange provider (fake), not snapshot files
2. /pair reads from the live exchange provider (fake), not snapshot files
3. /signal(/signals) reads from the live exchange provider (fake)
4. commands do NOT read data/scanner_results.json in realtime mode
5. provider failure produces a clear error message (never stale data)
6. multiple exchanges (binance, indodax, bybit) pass the same tests
7. per-symbol TTL cache prevents request spam on the exchange

All tests are offline: the exchange is a FakeProvider with canned
ticker + OHLCV data, exercised through the same code path the scanner
uses (PairAnalyzer._from_ohlcv + _score_and_rank).
"""

import json
import os
import time
from typing import Any

import pytest

from scripts.app_config import AppConfig
from scripts.realtime_market import RealtimeMarketData, RealtimeMarketError
from telegram.command_center import CommandCenter
from telegram.commands.detail import DetailCommand
from telegram.commands.market import MarketCommand
from telegram.commands.pair import PairCommand
from telegram.commands.signals import SignalsCommand
from telegram.context import CommandContext
from tests.conftest import FakeCCXTExchange, FakeExchangeManager, FakeProvider, FakeServices

SENTINEL = "SENTINEL_STALE_SNAPSHOT"


@pytest.fixture(autouse=True)
def _sandbox_data(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in a throwaway directory; plant a stale snapshot
    file so any accidental snapshot read is detectable."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    with open("data/scanner_results.json", "w") as f:
        json.dump({"generated": "2020-01-01T00:00:00Z", "pairs": [
            {"symbol": "BTC/USDT", "price": 99999.0, "signal": "STRONG BUY",
             "overall": 99.9, "marker": SENTINEL},
        ]}, f)


def _cfg(exchange: str = "binance", quote: str = "USDT",
         telegram_chat_id: str = "") -> AppConfig:
    return AppConfig(
        exchange=exchange,
        quote_currency=quote,
        timeframe="1h",
        telegram_chat_id=telegram_chat_id,
        account_balance=10000,
        max_positions=3,
        max_risk_per_trade_pct=2,
        scanner_threads=5,
        scanner_top_n=50,
        telegram_timeout=10,
        telegram_retry=3,
        min_rr=1.5,
        max_rr=5,
        min_probability=50,
        max_atr_pct=8,
        tp1_sell_pct=30,
        tp2_sell_pct=30,
        tp3_sell_pct=40,
        taker_fee=0.001,
        maker_fee=0.00075,
        slippage_bps=3,
    )


def _ctx(provider: FakeProvider, cfg: AppConfig | None = None) -> CommandContext:
    cfg = cfg or _cfg(exchange=provider.exchange, quote=provider.quote)
    mgr = FakeExchangeManager(provider)
    return CommandContext(
        config=cfg,
        logger=None,
        services=FakeServices(mgr, cfg),
        chat_id="12345",
        message_id=1,
        update_id=1,
        raw_text="",
        is_admin=True,
        test_mode=True,
    )


def _execute(cmd_cls: Any, ctx: CommandContext, args: str = "") -> str:
    return cmd_cls().execute(ctx, args)


# ---------------------------------------------------------------------------
#  Realtime sourcing (criteria 1-3)
# ---------------------------------------------------------------------------


class TestRealtimeSourcing:
    def test_detail_uses_provider_realtime(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(DetailCommand, _ctx(provider), "BTC")

        assert "BTC/USDT" in result
        assert "60000" in result                      # fake ticker price
        assert "RSI" in result and "EMA200" in result  # recomputed indicators
        assert "Data Time:" in result
        assert "Data Age:" in result
        assert SENTINEL not in result

    def test_pair_uses_provider_realtime(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(PairCommand, _ctx(provider), "BTC")

        assert "BTC/USDT" in result
        assert "60000" in result
        assert "Indicators" in result
        assert "Data Age:" in result
        assert SENTINEL not in result

    def test_signals_uses_provider_realtime(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(SignalsCommand, _ctx(provider))

        assert "BTC/USDT" in result or "ETH/USDT" in result
        assert "Data Age:" in result
        assert SENTINEL not in result

    def test_market_uses_provider_realtime(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(MarketCommand, _ctx(provider))

        assert "Market Overview" in result
        assert "Bullish" in result
        assert "Data Age:" in result
        assert SENTINEL not in result

    def test_indicators_recomputed_from_live_ohlcv(self) -> None:
        """Indicators must be derived from the freshly fetched OHLCV, not
        from any stored analysis: with no snapshot file present the
        command still produces full indicator output."""
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(DetailCommand, _ctx(provider), "BTC")

        assert "RSI:" in result
        assert "ADX:" in result
        assert "ATR:" in result
        assert "Trend:" in result
        assert "Signal:" in result
        assert "Recommendation:" in result
        assert "Source: binance · 1h" in result


# ---------------------------------------------------------------------------
#  No snapshot reads in realtime mode (criterion 4)
# ---------------------------------------------------------------------------


class TestNoSnapshotReads:
    def test_commands_never_read_scanner_snapshot(self) -> None:
        """Even with a bogus snapshot on disk, realtime output must be
        built purely from the live provider."""
        provider = FakeProvider(exchange="binance", quote="USDT")
        ctx = _ctx(provider)

        for cmd_cls, args in [
            (DetailCommand, "BTC"),
            (PairCommand, "ETH"),
            (SignalsCommand, ""),
            (MarketCommand, ""),
        ]:
            result = _execute(cmd_cls, ctx, args)
            assert SENTINEL not in result, f"{cmd_cls.meta.name} read snapshot!"
            assert "99999" not in result, f"{cmd_cls.meta.name} used stale price!"

    def test_snapshot_file_absent_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Realtime commands must not depend on snapshot files existing."""
        os.remove("data/scanner_results.json")
        provider = FakeProvider(exchange="binance", quote="USDT")
        ctx = _ctx(provider)
        for cmd_cls, args in [
            (DetailCommand, "BTC"),
            (PairCommand, "BTC"),
            (SignalsCommand, ""),
            (MarketCommand, ""),
        ]:
            ctx.services.realtime_market._chat_cooldown.pop(ctx.chat_id, None)
            result = _execute(cmd_cls, ctx, args)
            assert "BTC/USDT" in result or "Market Overview" in result or "Top Signals" in result
            assert "Realtime error" not in result


# ---------------------------------------------------------------------------
#  Provider failure (criterion 5)
# ---------------------------------------------------------------------------


class TestProviderFailure:
    def test_detail_failure_shows_clear_error(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT", fail=True)
        result = _execute(DetailCommand, _ctx(provider), "BTC")

        assert "Realtime error" in result
        assert "binance" in result
        assert "retry" in result.lower() or "moment" in result.lower()
        assert SENTINEL not in result
        assert "60000" not in result  # never show data from a failed fetch

    def test_pair_failure_shows_clear_error(self) -> None:
        provider = FakeProvider(exchange="bybit", quote="USDT", fail=True)
        result = _execute(PairCommand, _ctx(provider), "BTC")

        assert "Realtime error" in result
        assert "bybit" in result
        assert SENTINEL not in result

    def test_signals_failure_shows_clear_error(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT", fail=True)
        result = _execute(SignalsCommand, _ctx(provider))

        assert "Realtime error" in result
        assert "binance" in result
        assert SENTINEL not in result

    def test_market_failure_shows_clear_error(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT", fail=True)
        result = _execute(MarketCommand, _ctx(provider))

        assert "Realtime error" in result
        assert SENTINEL not in result

    def test_unknown_symbol_clear_error(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        result = _execute(PairCommand, _ctx(provider), "NONEXISTENT")

        assert "not found" in result
        assert "binance" in result


# ---------------------------------------------------------------------------
#  Multi-exchange (criterion 6)
# ---------------------------------------------------------------------------


class TestMultipleExchanges:
    @pytest.mark.parametrize("exchange,quote,expected_symbol", [
        ("binance", "USDT", "BTC/USDT"),
        ("bybit", "USDT", "BTC/USDT"),
        ("indodax", "IDR", "BTC/IDR"),
        ("tokocrypto", "USDT", "BTC/USDT"),
    ])
    def test_symbol_resolution_per_exchange(
        self, exchange: str, quote: str, expected_symbol: str,
    ) -> None:
        provider = FakeProvider(exchange=exchange, quote=quote)
        result = _execute(DetailCommand, _ctx(provider), "BTC")

        assert expected_symbol in result
        assert f"Source: {exchange} · 1h" in result
        assert SENTINEL not in result

    def test_indodax_idr_pair(self) -> None:
        provider = FakeProvider(exchange="indodax", quote="IDR")
        result = _execute(DetailCommand, _ctx(provider), "BTC")

        assert "BTC/IDR" in result
        assert "Source: indodax" in result

    def test_quote_aware_resolution_rejects_wrong_quote(self) -> None:
        provider = FakeProvider(exchange="indodax", quote="IDR")
        result = _execute(PairCommand, _ctx(provider), "BTC/USDT")

        assert "not found" in result
        assert "indodax" in result


# ---------------------------------------------------------------------------
#  Cache TTL (performance requirement)
# ---------------------------------------------------------------------------


class TestCacheTTL:
    def test_analysis_cached_per_symbol(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        rt = RealtimeMarketData(
            FakeExchangeManager(provider), timeframe="1h", analysis_ttl=3600,
            _public_exchange_factory=lambda name: FakeCCXTExchange(provider),
        )

        rt.analyze("BTC/USDT")
        rt.analyze("BTC/USDT")
        assert provider.ohlcv_calls == 1  # second call served from cache
        assert provider.ticker_calls == 1

        rt.analyze("ETH/USDT")            # different symbol -> new fetch
        rt.analyze("BTC/USDT")            # cache still valid per symbol
        assert provider.ohlcv_calls == 2

    def test_cache_expiry_refetches(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        rt = RealtimeMarketData(
            FakeExchangeManager(provider), timeframe="1h", analysis_ttl=0,
            _public_exchange_factory=lambda name: FakeCCXTExchange(provider),
        )

        rt.analyze("BTC/USDT")
        rt.analyze("BTC/USDT")
        assert provider.ohlcv_calls == 2  # TTL 0 -> every call refetches


class TestChatCooldown:
    def test_resolve_symbol_cooldown_blocks_rapid_calls(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        rt = RealtimeMarketData(
            FakeExchangeManager(provider), timeframe="1h",
            _public_exchange_factory=lambda name: FakeCCXTExchange(provider),
        )
        rt.resolve_symbol("BTC", chat_id="chat1")
        with pytest.raises(RealtimeMarketError, match="wait a few seconds"):
            rt.resolve_symbol("ETH", chat_id="chat1")
        rt._chat_cooldown["chat1"] = time.time() - 4.0
        rt.resolve_symbol("ETH", chat_id="chat1")

    def test_top_candidates_cooldown_blocks_rapid_calls(self) -> None:
        provider = FakeProvider(exchange="binance", quote="USDT")
        rt = RealtimeMarketData(
            FakeExchangeManager(provider), timeframe="1h",
            _public_exchange_factory=lambda name: FakeCCXTExchange(provider),
        )
        rt.top_candidates(chat_id="chat1")
        with pytest.raises(RealtimeMarketError, match="wait a few seconds"):
            rt.top_candidates(chat_id="chat1")


# ---------------------------------------------------------------------------
#  Dispatch level (registry routes /signal and /pair correctly)
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_registry_routes_aliases_to_realtime_commands(self) -> None:
        cc = CommandCenter(
            config=_cfg(telegram_chat_id="12345"), logger=None,
            services=FakeServices(FakeExchangeManager(
                FakeProvider(exchange="binance", quote="USDT"),
            ), _cfg()),
        )
        assert cc.registry.resolve("signal") is SignalsCommand
        assert cc.registry.resolve("pair") is PairCommand
        assert cc.registry.resolve("d") is DetailCommand
        assert cc.registry.resolve("mkt") is MarketCommand

    def test_dispatch_detail_realtime(self) -> None:
        cfg = _cfg(telegram_chat_id="12345")
        cc = CommandCenter(
            config=cfg, logger=None,
            services=FakeServices(FakeExchangeManager(
                FakeProvider(exchange="binance", quote="USDT"),
            ), cfg),
        )
        reply = cc.dispatch("12345", 1, 1, "/detail BTC")
        assert reply is not None
        assert "BTC/USDT" in reply
        assert "Data Age:" in reply
