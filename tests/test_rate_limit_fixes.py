"""Regression tests for the Indodax rate-limit / health-probe fixes.

Two problems found while running the paper bot on Indodax in Indonesia:

1. Health ``internet=FAIL`` on a perfectly working connection: the probe
   pinged ``api.binance.com``, which is geo-blocked by Indonesian ISPs
   (Kominfo). The probe is now exchange-independent.
2. Pipeline ``Scanner...FAILED 429 Too Many Requests``: the scanner issued
   one ``fetch_ohlcv()`` request per pair per cycle (~395 on Indodax),
   tripping the per-IP rate limit. Fixes: analyze only the top-N most
   liquid pairs, and serve candles from a file-backed TTL cache.
"""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import scripts.health as health_mod
from scripts.scanner import (
    MarketScanner,
    PairAnalyzer,
    PairRaw,
    TIMEFRAME,
    _cache_ohlcv,
    _cached_ohlcv,
    clear_ohlcv_cache,
)


def _fake_config(exchange: str = "binance", quote_currency: str = "USDT") -> SimpleNamespace:
    return SimpleNamespace(exchange=exchange, quote_currency=quote_currency)


# ===========================================================================
#  Health internet probe
# ===========================================================================


class TestCheckInternet:
    def test_uses_neutral_probes_never_binance(self) -> None:
        assert len(health_mod._INTERNET_PROBES) >= 2
        assert all("binance" not in u for u in health_mod._INTERNET_PROBES)
        with patch("scripts.health.requests.get", return_value=MagicMock()) as mock_get:
            ok, latency = health_mod._check_internet()
        assert ok is True
        assert latency >= 0.0
        # First probe succeeds -> loop breaks, and the URL probed is a
        # neutral endpoint, never a geo-blocked exchange host.
        assert mock_get.call_args_list[0].args[0] == health_mod._INTERNET_PROBES[0]

    def test_first_success_short_circuits(self) -> None:
        """One reachable probe is enough — later probes must not run."""
        with patch(
            "scripts.health.requests.get",
            side_effect=[MagicMock(), AssertionError("should not be reached")],
        ) as mock_get:
            ok, _ = health_mod._check_internet()
        assert ok is True
        assert mock_get.call_count == 1

    def test_all_probes_down_returns_fail(self) -> None:
        def _down(*_a: Any, **_k: Any) -> None:
            import requests
            raise requests.RequestException("down")

        with patch("scripts.health.requests.get", side_effect=_down):
            ok, latency = health_mod._check_internet()
        assert ok is False
        assert latency == 0.0


# ===========================================================================
#  Scanner OHLCV cache
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the cache file into tmp and reset module cache state."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    monkeypatch.setattr(
        "scripts.scanner.OHLCV_CACHE_PATH", os.path.join(tmp_path, "data", "scanner_ohlcv_cache.json"),
    )
    clear_ohlcv_cache()
    PairAnalyzer._thread_local = None


def _short_candles() -> list[list[Any]]:
    """A few candles (< MIN_CANDLES) — enough to exercise the cache path
    without indicator math (analysis returns status 'skipped')."""
    return [[1700000000000 + i * 3_600_000, 100.0, 101.0, 99.0, 100.5, 1000.0] for i in range(5)]


def _candles(n: int = 260) -> list[list[Any]]:
    start = 1_700_000_000_000
    out: list[list[Any]] = []
    for i in range(n):
        ts = start + i * 3_600_000
        o = 100.0 + i * 0.01
        c = o + 0.02
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        out.append([ts, o, h, l, c, 1000.0 + i])
    return out


class TestOhlcvCache:
    def test_analyze_fetches_once_then_serves_from_cache(self) -> None:
        fake_ccxt = MagicMock()
        fake_instance = MagicMock()
        fake_instance.fetch_ohlcv.return_value = _short_candles()
        fake_ccxt.okx.return_value = fake_instance

        pair = PairRaw(symbol="BTC/USDT", base="BTC")
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            first = PairAnalyzer.analyze(pair, "okx")
            second = PairAnalyzer.analyze(pair, "okx")

        assert fake_instance.fetch_ohlcv.call_count == 1
        assert first.status == second.status

    def test_cache_is_keyed_by_exchange(self) -> None:
        fake_ccxt = MagicMock()
        fake_instance = MagicMock()
        fake_instance.fetch_ohlcv.return_value = _short_candles()
        fake_ccxt.okx.return_value = fake_instance
        fake_ccxt.gate.return_value = MagicMock()

        pair = PairRaw(symbol="BTC/USDT", base="BTC")
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            PairAnalyzer.analyze(pair, "okx")
            # Same symbol on a DIFFERENT exchange must not be served the
            # okx candles — gate must still fetch its own data.
            PairAnalyzer.analyze(pair, "gate")

        assert fake_ccxt.okx.return_value.fetch_ohlcv.call_count == 1
        assert fake_ccxt.gate.return_value.fetch_ohlcv.call_count == 1

    def test_cached_ohlcv_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _cache_ohlcv("okx", "BTC/USDT", TIMEFRAME, _short_candles())
        key = _ohlcv_key("okx", "BTC/USDT")
        assert _cached_ohlcv("okx", "BTC/USDT") is not None
        # Age the entry beyond the TTL.
        monkeypatch.setattr("scripts.scanner.OHLCV_CACHE_TTL", 900)
        _ohlcv_cache_mod()[key]["fetched_at"] = time.time() - 901
        assert _cached_ohlcv("okx", "BTC/USDT") is None

    def test_cache_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.scanner.OHLCV_CACHE_MAX_ENTRIES", 3)
        for i in range(10):
            _cache_ohlcv("okx", f"PAIR{i:02d}/USDT", TIMEFRAME, _short_candles())
        cache = _ohlcv_cache_mod()
        assert len(cache) <= 3
        assert os.path.exists("data/scanner_ohlcv_cache.json")

    def test_error_path_does_not_cache(self) -> None:
        fake_ccxt = MagicMock()
        fake_instance = MagicMock()
        fake_instance.fetch_ohlcv.side_effect = Exception("rate limited")
        fake_ccxt.gate.return_value = fake_instance

        pair = PairRaw(symbol="BTC/USDT", base="BTC")
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}):
            result = PairAnalyzer.analyze(pair, "gate")

        assert result.status == "error"
        assert _cached_ohlcv("gate", "BTC/USDT") is None


def _ohlcv_key(exchange_name: str, symbol: str) -> str:
    return f"{exchange_name.lower()}:{symbol}"


def _ohlcv_cache_mod() -> dict[str, Any]:
    import scripts.scanner as scanner_mod
    return scanner_mod._ohlcv_cache


# ===========================================================================
#  Scanner — analyze only the top-N most liquid pairs (request cap)
# ===========================================================================


class TestTopNVolumeCap:
    @patch("scripts.scanner.MarketData")
    def test_run_fetches_ohlcv_for_at_most_top_n(
        self, mock_md: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        markets = [
            {"symbol": f"PAIR{i:02d}/USDT", "base": f"PAIR{i:02d}",
             "quote": "USDT", "spot": True, "active": True}
            for i in range(20)
        ]
        tickers = {
            f"PAIR{i:02d}/USDT": {"last": 100.0, "quoteVolume": 1_000_000.0,
                                  "percentage": 1.0, "high": 101.0, "low": 99.0}
            for i in range(20)
        }
        mock_ex = mock_md.return_value.exchange
        mock_md.return_value.fetch_markets.return_value = markets
        mock_md.return_value.fetch_tickers.return_value = tickers
        mock_ex.fetch_ohlcv.return_value = _candles()

        # Route OHLCV fetches through the SAME mocked exchange so no real
        # network call happens inside the analysis threads.
        monkeypatch.setattr(
            PairAnalyzer, "_get_exchange",
            classmethod(lambda cls, name: mock_ex),
        )
        monkeypatch.setattr("scripts.scanner.TOP_N", 5)

        scanner = MarketScanner(threads=2, config=_fake_config())
        scored, stats = scanner.run()

        # 20 liquid pairs capped to top 5 by volume.
        assert stats["volume_capped"] == 15
        assert mock_ex.fetch_ohlcv.call_count == 5
        assert len(scored) == 5

    @patch("scripts.scanner.MarketData")
    def test_no_cap_when_less_than_top_n(
        self, mock_md: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        markets = [
            {"symbol": f"PAIR{i:02d}/USDT", "base": f"PAIR{i:02d}",
             "quote": "USDT", "spot": True, "active": True}
            for i in range(3)
        ]
        tickers = {
            f"PAIR{i:02d}/USDT": {"last": 100.0, "quoteVolume": 1_000_000.0,
                                  "percentage": 1.0, "high": 101.0, "low": 99.0}
            for i in range(3)
        }
        mock_ex = mock_md.return_value.exchange
        mock_md.return_value.fetch_markets.return_value = markets
        mock_md.return_value.fetch_tickers.return_value = tickers
        mock_ex.fetch_ohlcv.return_value = _candles()

        monkeypatch.setattr(
            PairAnalyzer, "_get_exchange",
            classmethod(lambda cls, name: mock_ex),
        )
        monkeypatch.setattr("scripts.scanner.TOP_N", 50)

        scanner = MarketScanner(threads=2, config=_fake_config())
        scored, stats = scanner.run()

        assert stats["volume_capped"] == 0
        assert mock_ex.fetch_ohlcv.call_count == 3
        assert len(scored) == 3
