"""
Auto Market Scanner for ZetBot AI

Scans all Spot trading pairs (quoted in the configured quote currency,
default USDT) from the configured exchange — any of Binance, Bybit,
Tokocrypto, OKX, Gate, Kucoin, MEXC, or Indodax via EXCHANGE in .env —
calculates technical indicators, scores each pair, and ranks by
trading opportunity.

Usage::

    python -m scripts.scanner
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logging.getLogger("ZetBot").setLevel(logging.WARNING)

from bot.data import MarketData, NORMALIZED_COLUMNS
from bot.indicators import IndicatorEngine

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

MIN_CANDLES = 250          # minimum candles required for analysis (must match fetch limit)
OHLCV_LIMIT = MIN_CANDLES  # fetch this many candles (must match MIN_CANDLES)
TIMEFRAME = "1h"
TOP_N = 50
THREADS = 8
MIN_VOLUME_24H = 50_000  # skip pairs below $50K daily volume

# Weights for overall score (must sum to 1.0)
W_TREND = 0.30
W_MOMENTUM = 0.25
W_VOLUME = 0.15
W_VOLATILITY = 0.10
W_LIQUIDITY = 0.20

# ---------------------------------------------------------------------------
#  Filters
# ---------------------------------------------------------------------------

LEVERAGED_KEYWORDS = ["UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S"]

STABLECOINS = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "BUSD", "USTC", "USDD", "GUSD", "PAXG",
}


def _is_leveraged(base: str) -> bool:
    upper = base.upper()
    for kw in LEVERAGED_KEYWORDS:
        if kw in upper:
            return True
    return False


def _is_stablecoin(base: str) -> bool:
    return base.upper() in STABLECOINS


# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class PairRaw:
    """Raw market information for one trading pair."""
    symbol: str
    base: str
    price: float = 0.0
    volume_24h: float = 0.0
    change_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    # On-chain routing metadata — empty/"cex" for regular exchange pairs.
    venue: str = "cex"
    chain: str = ""
    token_address: str = ""
    quote_address: str = ""


@dataclass
class PairAnalysis:
    """Complete analysis result for one pair."""
    symbol: str
    base: str
    price: float
    volume_24h: float
    change_24h: float
    ema50: float
    ema100: float
    ema200: float
    rsi14: float
    adx14: float
    atr14: float
    volume_ma20: float
    highest_high_20: float
    lowest_low_20: float
    atr_pct: float
    relative_volume: float
    trend_alignment: str
    candle_count: int
    status: str = "ok"
    error: str = ""
    venue: str = "cex"


@dataclass
class ScoredPair:
    """Ranked pair with score and signal."""
    symbol: str
    base: str
    price: float
    volume_24h: float
    change_24h: float
    ema50: float
    ema100: float
    ema200: float
    rsi14: float
    adx14: float
    atr_pct: float
    relative_volume: float
    trend_alignment: str
    trend_score: float
    momentum_score: float
    volume_score: float
    volatility_score: float
    liquidity_score: float
    overall: float
    signal: str
    rank: int
    venue: str = "cex"


# ---------------------------------------------------------------------------
#  IndicatorAnalyzer
# ---------------------------------------------------------------------------


class IndicatorAnalyzer:
    """Calculate all technical indicators for a single pair's OHLCV."""

    @staticmethod
    def analyze(df: pd.DataFrame, price: float) -> dict[str, Any]:
        """Compute all indicators and return as a flat dict."""
        close = df["close"]
        volume = df["volume"]

        ema50_series = IndicatorEngine.ema(close, period=50)
        ema100_series = IndicatorEngine.ema(close, period=100)
        ema200_series = IndicatorEngine.ema(close, period=200)

        ema50 = float(ema50_series.iloc[-1])
        ema100 = float(ema100_series.iloc[-1])
        ema200 = float(ema200_series.iloc[-1])

        rsi14 = IndicatorEngine.rsi(close, period=14)
        adx14 = IndicatorEngine.adx(df, period=14)
        atr14 = IndicatorEngine.atr(df, period=14)

        volume_ma20 = float(volume.rolling(window=20).mean().iloc[-1])
        highest_high_20 = float(df["high"].rolling(window=20).max().iloc[-1])
        lowest_low_20 = float(df["low"].rolling(window=20).min().iloc[-1])

        atr_pct = (atr14 / price * 100.0) if price > 0 else 0.0
        rel_vol = (volume.iloc[-1] / volume_ma20) if volume_ma20 > 0 else 1.0

        # Trend alignment
        bullish_count = sum([price > ema50, price > ema100, price > ema200])
        bearish_count = sum([price < ema50, price < ema100, price < ema200])

        if bullish_count == 3 and ema50 > ema100 > ema200:
            trend_alignment = "BULLISH"
        elif bearish_count == 3 and ema50 < ema100 < ema200:
            trend_alignment = "BEARISH"
        elif bullish_count >= 2:
            trend_alignment = "BULLISH"
        elif bearish_count >= 2:
            trend_alignment = "BEARISH"
        else:
            trend_alignment = "MIXED"

        return {
            "ema50": ema50,
            "ema100": ema100,
            "ema200": ema200,
            "rsi14": rsi14,
            "adx14": adx14,
            "atr14": atr14,
            "volume_ma20": volume_ma20,
            "highest_high_20": highest_high_20,
            "lowest_low_20": lowest_low_20,
            "atr_pct": atr_pct,
            "relative_volume": rel_vol,
            "trend_alignment": trend_alignment,
            "candle_count": len(df),
        }


# ---------------------------------------------------------------------------
#  ScoreCalculator
# ---------------------------------------------------------------------------


class ScoreCalculator:
    """Compute individual dimension scores and overall score."""

    @staticmethod
    def trend_score(price: float, ema50: float, ema100: float, ema200: float,
                    alignment: str) -> float:
        bullish_count = sum([price > ema50, price > ema100, price > ema200])
        bearish_count = sum([price < ema50, price < ema100, price < ema200])

        if alignment == "BULLISH" and bullish_count == 3:
            return 90.0
        if bullish_count == 3:
            return 75.0
        if bullish_count == 2:
            return 60.0
        if bullish_count == 1:
            return 40.0
        if bearish_count == 3:
            return 15.0
        return 25.0

    @staticmethod
    def momentum_score(rsi: float, adx: float) -> float:
        rsi = max(0.0, min(100.0, rsi))

        if 40 <= rsi <= 50:
            base = 80.0
        elif 30 <= rsi < 40:
            base = 65.0
        elif 50 < rsi <= 60:
            base = 65.0
        elif 60 < rsi <= 70:
            base = 45.0
        elif rsi < 30:
            base = 30.0
        else:
            base = 20.0

        if adx >= 30:
            base = min(100.0, base + 15)
        elif adx >= 25:
            base = min(100.0, base + 10)
        elif adx >= 20:
            base = min(100.0, base + 5)

        return base

    @staticmethod
    def volume_score(relative_volume: float) -> float:
        rv = min(relative_volume, 3.0)
        return rv / 3.0 * 100.0

    @staticmethod
    def volatility_score(atr_pct: float) -> float:
        if atr_pct > 8.0:
            return 100.0
        if atr_pct > 5.0:
            return 80.0
        if atr_pct > 3.0:
            return 60.0
        if atr_pct > 1.5:
            return 40.0
        if atr_pct > 0.5:
            return 20.0
        return 10.0

    @staticmethod
    def liquidity_score(volume_24h: float) -> float:
        if volume_24h >= 1_000_000_000:
            return 100.0
        if volume_24h >= 100_000_000:
            return 90.0
        if volume_24h >= 10_000_000:
            return 75.0
        if volume_24h >= 1_000_000:
            return 55.0
        if volume_24h >= 100_000:
            return 30.0
        return 10.0

    @classmethod
    def overall(cls, trend: float, momentum: float, volume: float,
                volatility: float, liquidity: float) -> float:
        raw = (trend * W_TREND + momentum * W_MOMENTUM
               + volume * W_VOLUME + volatility * W_VOLATILITY
               + liquidity * W_LIQUIDITY)
        return max(0.0, min(100.0, raw))


# ---------------------------------------------------------------------------
#  Signal classifier
# ---------------------------------------------------------------------------


def classify_signal(score: float) -> str:
    if score >= 80:
        return "STRONG BUY"
    if score >= 60:
        return "BUY"
    if score >= 40:
        return "WATCHLIST"
    if score >= 20:
        return "NEUTRAL"
    return "AVOID"


# ---------------------------------------------------------------------------
#  PairAnalyzer
# ---------------------------------------------------------------------------


class PairAnalyzer:
    """Analyze a single trading pair end-to-end.

    Uses a thread-local ccxt exchange to avoid creating a new
    connection per call while keeping each thread independent.
    """

    _thread_local: Any = None

    @classmethod
    def _get_exchange(cls, exchange_name: str):
        """Return a thread-local ccxt instance for *exchange_name*.

        Cached per thread AND per exchange name (a dict on the
        thread-local, not a single slot) so that the same worker thread
        can safely be reused across scans of different exchanges (e.g.
        tests, or the pipeline running twice with a different EXCHANGE
        env value) without leaking a stale connection.

        Resolves the exchange via ``scripts.exchange_providers`` — the
        same registry used everywhere else — so aliases like
        'tokocrypto' (which rides on Binance's ccxt class) are handled
        consistently instead of a second hardcoded mapping here.
        """
        import ccxt
        from scripts.exchange_providers import get_provider_class

        if cls._thread_local is None:
            cls._thread_local = threading.local()
        if not hasattr(cls._thread_local, "exchanges"):
            cls._thread_local.exchanges = {}
        cache = cls._thread_local.exchanges

        if exchange_name not in cache:
            ccxt_name = get_provider_class(exchange_name)().ccxt_name
            exchange_cls = getattr(ccxt, ccxt_name, None)
            if exchange_cls is None:
                raise ValueError(
                    f"CCXT has no exchange '{ccxt_name}' (for '{exchange_name}')"
                )
            cache[exchange_name] = exchange_cls({
                "enableRateLimit": True,
                "timeout": 15000,
            })
        return cache[exchange_name]

    @staticmethod
    def _from_ohlcv(pair: PairRaw, raw: list) -> PairAnalysis:
        """Build a PairAnalysis from already-fetched OHLCV candles.

        This is the single shared scoring path — identical indicator
        math (``IndicatorAnalyzer.analyze``) runs regardless of whether
        the candles came from a CEX (ccxt) or a DEX (on-chain
        providers), so a token scanned on-chain is ranked by exactly
        the same rules as a Binance pair, not a parallel implementation.
        """
        import pandas as pd

        if not raw or len(raw) < MIN_CANDLES:
            return PairAnalysis(
                symbol=pair.symbol, base=pair.base,
                price=pair.price, volume_24h=pair.volume_24h,
                change_24h=pair.change_24h,
                ema50=0, ema100=0, ema200=0,
                rsi14=0, adx14=0, atr14=0,
                volume_ma20=0, highest_high_20=0, lowest_low_20=0,
                atr_pct=0, relative_volume=0,
                trend_alignment="", candle_count=len(raw) if raw else 0,
                status="skipped",
                error=f"only {len(raw)} candles (< {MIN_CANDLES})",
                venue=pair.venue,
            )

        df = pd.DataFrame(raw, columns=NORMALIZED_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        ind = IndicatorAnalyzer.analyze(df, pair.price)

        return PairAnalysis(
            symbol=pair.symbol, base=pair.base,
            price=pair.price, volume_24h=pair.volume_24h,
            change_24h=pair.change_24h,
            ema50=ind["ema50"], ema100=ind["ema100"],
            ema200=ind["ema200"],
            rsi14=ind["rsi14"], adx14=ind["adx14"],
            atr14=ind["atr14"],
            volume_ma20=ind["volume_ma20"],
            highest_high_20=ind["highest_high_20"],
            lowest_low_20=ind["lowest_low_20"],
            atr_pct=ind["atr_pct"],
            relative_volume=ind["relative_volume"],
            trend_alignment=ind["trend_alignment"],
            candle_count=ind["candle_count"],
            status="ok",
            venue=pair.venue,
        )

    @classmethod
    def analyze(cls, pair: PairRaw, exchange_name: str) -> PairAnalysis:
        """Fetch OHLCV from the CEX (ccxt), calculate indicators, return analysis."""
        try:
            exchange = cls._get_exchange(exchange_name)
            raw = exchange.fetch_ohlcv(
                symbol=pair.symbol,
                timeframe=TIMEFRAME,
                limit=OHLCV_LIMIT,
            )
            return cls._from_ohlcv(pair, raw)
        except Exception as e:
            return PairAnalysis(
                symbol=pair.symbol, base=pair.base,
                price=pair.price, volume_24h=pair.volume_24h,
                change_24h=pair.change_24h,
                ema50=0, ema100=0, ema200=0,
                rsi14=0, adx14=0, atr14=0,
                volume_ma20=0, highest_high_20=0, lowest_low_20=0,
                atr_pct=0, relative_volume=0,
                trend_alignment="", candle_count=0,
                status="error", error=f"[{exchange_name}] {e}",
                venue=pair.venue,
            )

    @classmethod
    def analyze_onchain(cls, pair: PairRaw, config: Any) -> PairAnalysis:
        """Fetch OHLCV from the on-chain provider for *pair*'s chain,
        then reuse the exact same indicator/scoring path as ``analyze()``.
        """
        try:
            from scripts.onchain_providers import get_onchain_provider  # noqa: PLC0415

            provider = get_onchain_provider(pair.chain, config)
            raw = provider.fetch_ohlcv(pair.token_address, TIMEFRAME, OHLCV_LIMIT)
            return cls._from_ohlcv(pair, raw)
        except Exception as e:
            return PairAnalysis(
                symbol=pair.symbol, base=pair.base,
                price=pair.price, volume_24h=pair.volume_24h,
                change_24h=pair.change_24h,
                ema50=0, ema100=0, ema200=0,
                rsi14=0, adx14=0, atr14=0,
                volume_ma20=0, highest_high_20=0, lowest_low_20=0,
                atr_pct=0, relative_volume=0,
                trend_alignment="", candle_count=0,
                status="error", error=str(e),
                venue=pair.venue,
            )


# ---------------------------------------------------------------------------
#  MarketScanner (orchestrator)
# ---------------------------------------------------------------------------


class MarketScanner:
    """Orchestrate the multi-pair scan."""

    def __init__(self, threads: int = THREADS, config: Any = None) -> None:
        if config is None:
            from scripts.app_config import load_config  # noqa: PLC0415
            config = load_config()
        self.config = config
        exchange_raw = getattr(config, "exchange", "")
        if not exchange_raw:
            raise ValueError(
                "MarketScanner: no exchange configured. Set EXCHANGE in .env "
                "or pass a config with a non-empty .exchange attribute."
            )
        self.exchange_name: str = exchange_raw.lower()
        self.quote_currency: str = (
            getattr(config, "quote_currency", "") or "USDT"
        ).upper()
        self.md = MarketData(exchange_name=self.exchange_name)
        self.threads = threads
        self.pair_raws: list[PairRaw] = []

    def fetch_markets(self) -> list[PairRaw]:
        """Fetch and filter all Spot markets quoted in ``self.quote_currency``
        from the configured exchange (``self.exchange_name``)."""
        raw_markets = self.md.exchange.fetch_markets()
        pairs: list[PairRaw] = []
        for m in raw_markets:
            if m.get("spot") is not True:
                continue
            if m.get("quote") != self.quote_currency:
                continue
            if m.get("active") is not True:
                continue
            base = m["base"]
            if _is_leveraged(base) or _is_stablecoin(base):
                continue
            pairs.append(PairRaw(symbol=m["symbol"], base=base))
        return pairs

    def attach_tickers(self, pairs: list[PairRaw]) -> None:
        """Attach 24h ticker data to all pairs."""
        tickers = self.md.exchange.fetch_tickers()
        for p in pairs:
            t = tickers.get(p.symbol)
            if t is None:
                continue
            p.price = float(t.get("last", 0) or 0)
            p.volume_24h = float(t.get("quoteVolume", 0) or 0)
            p.change_24h = float(t.get("percentage", 0) or 0)
            p.high_24h = float(t.get("high", 0) or 0)
            p.low_24h = float(t.get("low", 0) or 0)

    def analyze_all(self, pairs: list[PairRaw]) -> list[PairAnalysis]:
        """Analyze all pairs in parallel with progress."""
        results: list[PairAnalysis] = []
        total = len(pairs)
        log_interval = max(1, total // 20)
        completed = 0
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            fut_map = {
                pool.submit(PairAnalyzer.analyze, p, self.exchange_name): p
                for p in pairs
            }
            for f in as_completed(fut_map):
                results.append(f.result())
                completed += 1
                if completed % log_interval == 0 or completed == total:
                    pct = completed * 100 // total
                    print(f"\r        Progress : {pct:3d}% "
                          f"({completed}/{total})", end="", flush=True)
        print()
        return results

    # ------------------------------------------------------------------
    #  On-chain (DEX) pairs — separate data source, same downstream path
    # ------------------------------------------------------------------

    def fetch_onchain_pairs(self) -> list[PairRaw]:
        """Build PairRaw entries from the on-chain watchlist, with live
        price/volume pulled from each token's DEX pool (DexScreener/
        Jupiter, via onchain_providers) — the on-chain equivalent of
        ``fetch_markets()`` + ``attach_tickers()`` combined, since a
        watchlist entry has no price until queried.
        """
        from scripts.onchain_providers import get_onchain_provider
        from scripts.onchain_watchlist import load_onchain_watchlist

        entries = load_onchain_watchlist(self.config)
        pairs: list[PairRaw] = []
        for entry in entries:
            try:
                provider = get_onchain_provider(entry.chain, self.config)
                ticker = provider.get_ticker(entry.token_address)
                if not ticker or not ticker.get("price_usd"):
                    continue
                base = entry.symbol.split("/")[0] if "/" in entry.symbol else entry.symbol
                pairs.append(PairRaw(
                    symbol=entry.symbol,
                    base=base,
                    price=float(ticker.get("price_usd") or 0),
                    volume_24h=float(ticker.get("volume_24h_usd") or 0),
                    change_24h=float(ticker.get("price_change_24h") or 0),
                    venue="onchain",
                    chain=entry.chain,
                    token_address=entry.token_address,
                    quote_address=entry.quote_address,
                ))
            except Exception as exc:
                print(f"        [onchain] skipped {entry.symbol} ({entry.chain}): {exc}")
        return pairs

    def analyze_onchain_all(self, pairs: list[PairRaw]) -> list[PairAnalysis]:
        """Same shape as ``analyze_all`` but routes each pair through
        its on-chain provider instead of ccxt. Kept as a separate,
        smaller thread pool since on-chain watchlists are expected to
        be far shorter than a full CEX market scan (tens, not hundreds,
        of tokens) — and to avoid hammering public RPC/API rate limits.
        """
        results: list[PairAnalysis] = []
        if not pairs:
            return results
        with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
            fut_map = {
                pool.submit(PairAnalyzer.analyze_onchain, p, self.config): p
                for p in pairs
            }
            for f in as_completed(fut_map):
                results.append(f.result())
        return results

    def run(self) -> tuple[list[PairAnalysis], dict[str, int]]:
        """Full scan pipeline. Returns (scored_pairs, stats dict)."""
        stats: dict[str, int] = {}

        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — AUTO MARKET SCANNER")
        print(f"  {'=' * 78}")
        print(f"  Exchange   : {self.exchange_name}")
        print(f"  Quote      : {self.quote_currency}")
        print(f"  Timeframe  : {TIMEFRAME}")
        print(f"  OHLCV limit: {OHLCV_LIMIT}")
        print(f"  Threads    : {self.threads}")
        if getattr(self.config, "onchain_enabled", False):
            print(f"  On-chain   : enabled ({self.config.onchain_chains or 'no chains configured'})")
        print()

        # 1. Markets
        print("  [1/4] Fetching markets … ", end="", flush=True)
        all_raw = self.md.exchange.fetch_markets()
        stats["total_markets"] = len(all_raw)
        raw_pairs = self.fetch_markets()
        stats["usdt_pairs"] = len(raw_pairs)
        print(f"{len(raw_pairs)} {self.quote_currency} pairs")

        # 2. Tickers
        print("  [2/4] Fetching tickers … ", end="", flush=True)
        self.attach_tickers(raw_pairs)
        print("done")
        stats["no_price"] = len([p for p in raw_pairs if p.price == 0])
        valid_pairs = [p for p in raw_pairs if p.price > 0]

        # Filter by minimum volume
        liquid_pairs = [p for p in valid_pairs if p.volume_24h >= MIN_VOLUME_24H]
        stats["low_volume"] = len(valid_pairs) - len(liquid_pairs)
        print(f"        Low vol : {stats['low_volume']} (< ${MIN_VOLUME_24H:,.0f}/d)")

        # 3. OHLCV & indicators
        print(f"  [3/4] Analyzing {len(liquid_pairs)} pairs "
              f"({self.threads} threads) …", flush=True)
        t0 = time.time()
        analyses = self.analyze_all(liquid_pairs)
        ohlcv_elapsed = time.time() - t0

        ok = [a for a in analyses if a.status == "ok"]
        skipped = [a for a in analyses if a.status == "skipped"]
        errors = [a for a in analyses if a.status == "error"]
        stats["analyzed"] = len(ok)
        stats["skipped"] = len(skipped)
        stats["errors"] = len(errors)
        print(f"        OK      : {len(ok)}")
        print(f"        Skipped : {len(skipped)} (< 250 candles)")
        print(f"        Errors  : {len(errors)}")
        print(f"        Time    : {ohlcv_elapsed:.1f}s")

        # 3b. On-chain pairs — same indicator/scoring path, merged into
        # the same `ok` list so ranking (and the scanner_results.json
        # output every downstream command reads) is a single unified
        # list, not two separate scans.
        onchain_pairs = self.fetch_onchain_pairs()
        stats["onchain_pairs"] = len(onchain_pairs)
        if onchain_pairs:
            print(f"  [3b/4] Analyzing {len(onchain_pairs)} on-chain pairs …", flush=True)
            onchain_analyses = self.analyze_onchain_all(onchain_pairs)
            onchain_ok = [a for a in onchain_analyses if a.status == "ok"]
            stats["onchain_analyzed"] = len(onchain_ok)
            stats["onchain_errors"] = len(
                [a for a in onchain_analyses if a.status == "error"],
            )
            print(f"        OK      : {len(onchain_ok)}")
            print(f"        Errors  : {stats['onchain_errors']}")
            ok = ok + onchain_ok
            self._write_onchain_symbol_map(onchain_pairs)

        # 4. Score & rank
        print("  [4/4] Scoring … ", end="", flush=True)
        scored = _score_and_rank(ok)
        stats["scored"] = len(scored)
        print(f"{len(scored)} pairs scored")
        print()

        return scored, stats

    @staticmethod
    def _write_onchain_symbol_map(pairs: list[PairRaw]) -> None:
        """Persist symbol → chain/contract routing info for on-chain
        pairs. Separate from scanner_results.json on purpose: it's
        execution-routing metadata (needed only when actually placing a
        trade), not scan/ranking output — keeping it out of the main
        results file avoids changing that file's consumers.
        """
        path = "data/onchain_symbol_map.json"
        try:
            mapping = {
                p.symbol: {
                    "chain": p.chain,
                    "token_address": p.token_address,
                    "quote_address": p.quote_address,
                }
                for p in pairs
            }
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                json.dump(mapping, f, indent=2)
        except OSError as exc:
            print(f"        [onchain] failed to write symbol map: {exc}")


# ---------------------------------------------------------------------------
#  Scoring & ranking
# ---------------------------------------------------------------------------


def _score_and_rank(analyses: list[PairAnalysis]) -> list[ScoredPair]:
    scored: list[ScoredPair] = []
    for a in analyses:
        ts = ScoreCalculator.trend_score(a.price, a.ema50, a.ema100, a.ema200,
                                          a.trend_alignment)
        ms = ScoreCalculator.momentum_score(a.rsi14, a.adx14)
        vs = ScoreCalculator.volume_score(a.relative_volume)
        vls = ScoreCalculator.volatility_score(a.atr_pct)
        ls = ScoreCalculator.liquidity_score(a.volume_24h)
        overall = ScoreCalculator.overall(ts, ms, vs, vls, ls)
        signal = classify_signal(overall)

        scored.append(ScoredPair(
            symbol=a.symbol, base=a.base,
            price=a.price, volume_24h=a.volume_24h,
            change_24h=a.change_24h,
            ema50=a.ema50, ema100=a.ema100, ema200=a.ema200,
            rsi14=a.rsi14, adx14=a.adx14,
            atr_pct=a.atr_pct,
            relative_volume=a.relative_volume,
            trend_alignment=a.trend_alignment,
            trend_score=round(ts, 1),
            momentum_score=round(ms, 1),
            volume_score=round(vs, 1),
            volatility_score=round(vls, 1),
            liquidity_score=round(ls, 1),
            overall=round(overall, 1),
            signal=signal,
            rank=0,
            venue=a.venue,
        ))

    scored.sort(key=lambda s: s.overall, reverse=True)
    for i, s in enumerate(scored, 1):
        s.rank = i

    return scored


# ---------------------------------------------------------------------------
#  Report generation
# ---------------------------------------------------------------------------


class ScannerReport:
    """Generate console and file output."""

    @staticmethod
    def console(scored: list[ScoredPair], elapsed: float,
                total_markets: int, filtered: int,
                analyzed: int) -> None:
        """Print ranked table to console."""
        top = scored[:TOP_N]

        print(f"  {'=' * 78}")
        print(f"  AUTO MARKET SCANNER — RESULTS")
        print(f"  {'=' * 78}")
        print(f"  Total markets       : {total_markets}")
        print(f"  Markets filtered    : {filtered}")
        print(f"  Markets analyzed    : {analyzed}")
        print(f"  Execution time      : {elapsed:.1f}s")
        print(f"  Generated           : "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"  {'=' * 78}")
        print()

        hdr = (
            f"  {'#':>3s} {'Pair':>12s} {'Price':>10s} "
            f"{'Vol 24h':>10s} {'EMA Dir':>7s} {'RSI':>5s} "
            f"{'ADX':>5s} {'ATR%':>6s} {'RelVol':>6s} "
            f"{'Score':>5s} {'Signal':>12s}"
        )
        print(hdr)
        print(f"  {'-' * (len(hdr) - 2)}")

        for s in top:
            direction = s.trend_alignment[:4] if s.trend_alignment else "?"
            print(
                f"  {s.rank:3d} {s.symbol:>12s} {s.price:>10.4f} "
                f"{_fmt_vol(s.volume_24h):>10s} {direction:>7s} "
                f"{s.rsi14:5.1f} {s.adx14:5.1f} "
                f"{s.atr_pct:6.2f} {s.relative_volume:6.2f} "
                f"{s.overall:5.1f} {s.signal:>12s}"
            )

        print()
        print(f"  ({len(scored)} total pairs scored, showing top {TOP_N})")
        print(f"  {'=' * 78}")
        print()

        # Summary counts by signal
        signal_counts: dict[str, int] = {}
        for s in scored:
            signal_counts[s.signal] = signal_counts.get(s.signal, 0) + 1
        print("  Signal Distribution:")
        for sig in ["STRONG BUY", "BUY", "WATCHLIST", "NEUTRAL", "AVOID"]:
            cnt = signal_counts.get(sig, 0)
            print(f"    {sig:>12s} : {cnt}")
        print()

    @staticmethod
    def to_csv(scored: list[ScoredPair], path: str) -> None:
        """Write all results to CSV."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "rank", "symbol", "base", "price", "volume_24h", "change_24h",
            "ema50", "ema100", "ema200", "rsi14", "adx14", "atr_pct",
            "relative_volume", "trend_alignment",
            "trend_score", "momentum_score", "volume_score",
            "volatility_score", "liquidity_score", "overall", "signal",
            "venue",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for s in scored:
                w.writerow(asdict(s))
        print(f"  CSV exported : {path}")

    @staticmethod
    def to_json(scored: list[ScoredPair], path: str,
                exchange_name: str) -> None:
        """Write all results as JSON."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "exchange": exchange_name,
            "timeframe": TIMEFRAME,
            "total_pairs": len(scored),
            "pairs": [asdict(s) for s in scored],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  JSON export : {path}")

    @staticmethod
    def to_watchlist(scored: list[ScoredPair], path: str,
                      exchange_name: str, top_n: int = 10) -> None:
        """Write top-N watchlist as a simple text file."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write("ZETBOT AI — WATCHLIST\n")
            f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Exchange: {exchange_name}  Timeframe: {TIMEFRAME}\n")
            f.write(f"{'=' * 50}\n\n")
            for s in scored[:top_n]:
                f.write(
                    f"{s.rank:3d}. {s.symbol:>10s}  "
                    f"${s.price:<10.4f}  Score: {s.overall:<5.1f}  "
                    f"Signal: {s.signal}\n"
                )
        print(f"  Watchlist   : {path}")


def _fmt_vol(vol: float) -> str:
    """Format a volume figure for human display."""
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.2f}B"
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"${vol / 1_000:.1f}K"
    return f"${vol:.0f}"


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main(config: Any) -> None:
    t0 = time.time()

    scanner = MarketScanner(threads=THREADS, config=config)
    scored, stats = scanner.run()
    total = len(scored)

    if total == 0:
        print("  No pairs scored.  Exiting.")
        return

    elapsed = time.time() - t0

    # Report
    report = ScannerReport()
    total_mkts = stats.get("total_markets", 0)
    filtered = total_mkts - stats.get("usdt_pairs", 0)
    report.console(scored, elapsed, total_mkts, filtered, total)

    csv_path = "data/scanner_results.csv"
    report.to_csv(scored, csv_path)

    json_path = "data/scanner_results.json"
    report.to_json(scored, json_path, exchange_name=scanner.exchange_name)

    watchlist_path = "data/watchlist.txt"
    report.to_watchlist(scored, watchlist_path, scanner.exchange_name)

    print(f"\n  Completed at : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Duration     : {elapsed:.1f}s")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    from scripts.app_config import load_config  # noqa: PLC0415
    main(config=load_config())
