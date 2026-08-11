"""Realtime market data service for Telegram user-facing commands.

Every Telegram command that displays market information (price,
indicators, signals, trends, volume, recommendations) MUST source its
data from the live exchange provider through this service — never from
the stale snapshot files the scanner pipeline writes
(``scanner_results.json``, ``decision_results.json``,
``risk_results.json``, ``trade_plan.json``).

Design:

- Uses the SAME indicator/scoring code path as the scanner
  (``scripts.scanner``: ``PairAnalyzer._from_ohlcv`` +
  ``_score_and_rank``) so the numbers a user sees in Telegram are
  exactly what the trading engine would compute right now.
- Works for EVERY exchange supported by the provider abstraction
  (Binance, Bybit, Tokocrypto, OKX, Gate, Kucoin, MEXC, Indodax) —
  symbol resolution is quote-currency aware (e.g. ``BTC`` resolves to
  ``BTC/IDR`` on Indodax, ``BTC/USDT`` elsewhere).
- Short per-symbol TTL cache (default 45 s) so repeated Telegram spam
  cannot hammer the exchange, while still never displaying data older
  than the TTL.
- Explicit failure contract: on any exchange failure a
  ``RealtimeMarketError`` is raised with a user-presentable message.
  Callers MUST show the error — never silently fall back to snapshot
  files, and never label stale data as realtime.

Every result carries ``fetched_at`` (epoch seconds of the underlying
exchange fetch) so commands can render the transparency footer:

    Data Time: 2026-08-08 00:15:23 WIB
    Data Age: 3 sec
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bot.data import get_cached_public_exchange
from scripts.scanner import (
    MIN_CANDLES,
    OHLCV_LIMIT,
    PairAnalyzer,
    PairRaw,
    _score_and_rank,
)

_log = logging.getLogger("ZetBot")

_WIB = timezone(timedelta(hours=7))

# ---------------------------------------------------------------------------
#  Tunables
# ---------------------------------------------------------------------------

TICKER_TTL_SEC = 30.0      # per-symbol ticker cache
TICKERS_LIST_TTL_SEC = 30.0  # full fetch_tickers() result cache (for /signals,/market)
ANALYSIS_TTL_SEC = 45.0    # per-symbol analysis cache (ticker + OHLCV + indicators)
DEFAULT_TOP_CANDIDATES = 20
DEFAULT_MARKET_TOP_N = 16
MAX_WORKERS = 8


class RealtimeMarketError(Exception):
    """User-facing failure: exchange unreachable / no data / bad symbol.

    ``str(exc)`` is safe to render directly in a Telegram reply and
    already includes the exchange name. Never caught silently.
    """


# ---------------------------------------------------------------------------
#  Result type
# ---------------------------------------------------------------------------


@dataclass
class RealtimeAnalysis:
    """Full realtime analysis for one symbol (mirrors scanner ScoredPair)."""

    symbol: str
    base: str
    exchange: str
    timeframe: str
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
    overall: float
    signal: str
    recommendation: str
    venue: str
    fetched_at: float  # epoch seconds of the exchange fetch


def recommendation_for(signal: str) -> str:
    """Human recommendation derived from the realtime signal.

    Uses the decision engine's terminology (STRONG BUY / GOOD /
    WATCHLIST / NEUTRAL / AVOID) so Telegram wording stays consistent
    with the rest of the system.
    """
    mapping = {
        "STRONG BUY": "STRONG BUY",
        "BUY": "GOOD",
        "WATCHLIST": "WATCHLIST",
        "NEUTRAL": "NEUTRAL",
        "AVOID": "AVOID",
    }
    return mapping.get(signal, signal)


# ---------------------------------------------------------------------------
#  Transparency helpers
# ---------------------------------------------------------------------------


def fmt_wib(ts: float) -> str:
    """Format an epoch timestamp as WIB: ``2026-08-08 00:15:23 WIB``."""
    return datetime.fromtimestamp(ts, _WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def fmt_age(ts: float, now: Optional[float] = None) -> str:
    """Human-readable age of a data point, e.g. ``3 sec`` / ``1 min 5 sec``."""
    secs = int(max(0, (now if now is not None else time.time()) - ts))
    if secs < 60:
        return f"{secs} sec"
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} sec"
    return f"{secs // 3600} hr {secs % 3600 // 60} min"


def data_footer(analysis: RealtimeAnalysis) -> str:
    """Transparency footer: source, data time and data age."""
    return (
        f"Data Time: {fmt_wib(analysis.fetched_at)}\n"
        f"Data Age: {fmt_age(analysis.fetched_at)}\n"
        f"Source: {analysis.exchange} · {analysis.timeframe}"
    )


# ---------------------------------------------------------------------------
#  Service
# ---------------------------------------------------------------------------


class RealtimeMarketData:
    """Fetches live ticker + OHLCV and recomputes indicators on demand.

    Not exchange-specific: resolves the active provider from an
    ``ExchangeManager``, so it follows runtime switches via
    ``/exchange`` and works for every supported exchange.
    """

    _thread_local = threading.local()

    def __init__(
        self,
        exchange_manager: Any,
        timeframe: str = "1h",
        analysis_ttl: float = ANALYSIS_TTL_SEC,
        ticker_ttl: float = TICKER_TTL_SEC,
        tickers_list_ttl: float = TICKERS_LIST_TTL_SEC,
        _public_exchange_factory: Any = None,
    ) -> None:
        self._mgr = exchange_manager
        self._timeframe = timeframe or "1h"
        self._analysis_ttl = analysis_ttl
        self._ticker_ttl = ticker_ttl
        self._tickers_list_ttl = tickers_list_ttl
        self._analysis_cache: dict[str, tuple[float, RealtimeAnalysis]] = {}
        self._ticker_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._tickers_list_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._lock = threading.Lock()
        self._public_exchange_factory = _public_exchange_factory or get_cached_public_exchange
        self._chat_cooldown: dict[str, float] = {}
        self._chat_cooldown_sec = 3.0

    # -- Active exchange context ------------------------------------------

    @property
    def exchange_name(self) -> str:
        return self._mgr.name

    @property
    def quote_currency(self) -> str:
        quote = getattr(self._mgr, "quote_currency", "")
        return (quote or "USDT").upper()

    def _provider(self) -> Any:
        """Active provider from the injected ExchangeManager.

        The manager is the single source of truth for providers (it owns
        the runtime switch via ``/exchange``, credentials, and quote
        overrides). Delegating through it — instead of constructing a
        provider from the class registry here — keeps the service
        testable and guarantees the same connection pool as the rest of
        the bot.
        """
        return self._mgr.get_provider()

    def _exchange_call(self, fn: Any, label: str) -> Any:
        """Run a raw ccxt call through the shared retry/backoff helper.

        Realtime commands used to call the cached ccxt client directly
        (``get_cached_public_exchange(...).fetch_ticker(...)``), which has
        no retry of its own. On a cold start the first ``/detail`` /
        ``/signals`` / ``/market`` would fail and only succeed on a retry —
        the same class of failure as the scanner's ``fetch_markets()``.
        Routing through ``exchange_call_with_retry`` fixes that for every
        configured exchange without hardcoding any name.
        """
        from scripts.exchange_providers import exchange_call_with_retry

        return exchange_call_with_retry(
            fn, label=label, exchange=self.exchange_name,
        )

    def _check_chat_cooldown(self, chat_id: str | None) -> None:
        now = time.time()
        if not chat_id:
            return
        last = self._chat_cooldown.get(chat_id, 0.0)
        if now - last < self._chat_cooldown_sec:
            raise RealtimeMarketError(
                "Rate limit: please wait a few seconds before requesting "
                "market data again."
            )
        self._chat_cooldown[chat_id] = now

    # -- Symbol resolution -------------------------------------------------

    def resolve_symbol(self, query: str, chat_id: str | None = None) -> str:
        """Resolve a user query (``BTC``, ``BTC/USDT``) to a real market
        symbol on the ACTIVE exchange, quote-currency aware."""
        self._check_chat_cooldown(chat_id)
        q = (query or "").strip().upper().replace(" ", "")
        if not q:
            raise RealtimeMarketError("No symbol provided.")
        quote = self.quote_currency

        exchange = self._public_exchange_factory(self.exchange_name)
        markets = exchange.load_markets()
        if not markets:
            raise RealtimeMarketError(
                f"Exchange '{self.exchange_name}': could not load market "
                "list from the exchange. Please retry in a moment."
            )

        symbols = set(markets.keys())
        if q in symbols:
            return q
        if "/" not in q:
            if f"{q}/{quote}" in symbols:
                return f"{q}/{quote}"
            for sym in symbols:
                if sym.startswith(q + "/"):
                    return sym
        raise RealtimeMarketError(
            f"Symbol '{query}' not found on {self.exchange_name} "
            f"(quote {quote}). Try e.g. /detail {q}/{quote}."
        )

    # -- Ticker (per-symbol, TTL-cached) -----------------------------------

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Live ticker for one symbol. Raises on empty/failed response."""
        now = time.time()
        with self._lock:
            cached = self._ticker_cache.get(symbol)
            if cached and now - cached[0] < self._ticker_ttl:
                return cached[1]

        ticker = self._exchange_call(
            lambda: self._public_exchange_factory(self.exchange_name).fetch_ticker(symbol),
            label=f"fetch_ticker({symbol})",
        )
        if not ticker or not ticker.get("last"):
            raise RealtimeMarketError(
                f"Exchange '{self.exchange_name}': failed to fetch live "
                f"ticker for {symbol}. No stale data is shown — please "
                "retry in a moment."
            )
        with self._lock:
            self._ticker_cache[symbol] = (time.time(), ticker)
        return ticker

    # -- Full analysis (ticker + OHLCV + indicators + signal) --------------

    def analyze(self, symbol: str) -> RealtimeAnalysis:
        """Recompute the full indicator analysis RIGHT NOW for *symbol*.

        Uses the scanner's exact indicator math (``PairAnalyzer`` +
        ``_score_and_rank``) so Telegram output is identical to what the
        trading engine would compute from the same candles.
        """
        now = time.time()
        with self._lock:
            cached = self._analysis_cache.get(symbol)
            if cached and now - cached[0] < self._analysis_ttl:
                return cached[1]

        ticker = self.fetch_ticker(symbol)
        fetched_at = time.time()

        exchange = self._public_exchange_factory(self.exchange_name)
        raw = self._exchange_call(
            lambda: exchange.fetch_ohlcv(
                symbol, timeframe=self._timeframe, limit=OHLCV_LIMIT,
            ),
            label=f"fetch_ohlcv({symbol})",
        )
        if not raw or len(raw) < MIN_CANDLES:
            raise RealtimeMarketError(
                f"Exchange '{self.exchange_name}': no OHLCV candles for "
                f"{symbol} on timeframe '{self._timeframe}' "
                f"(need >= {MIN_CANDLES}, got {len(raw) if raw else 0}). "
                "No stale data is shown — please retry in a moment."
            )

        base = symbol.split("/")[0] if "/" in symbol else symbol
        pair = PairRaw(
            symbol=symbol,
            base=base,
            price=float(ticker.get("last", 0) or 0),
            volume_24h=float(ticker.get("quoteVolume", 0) or 0),
            change_24h=float(ticker.get("percentage", 0) or 0),
            high_24h=float(ticker.get("high", 0) or 0),
            low_24h=float(ticker.get("low", 0) or 0),
        )
        analysis = PairAnalyzer._from_ohlcv(pair, raw)
        if analysis.status != "ok":
            raise RealtimeMarketError(
                f"Exchange '{self.exchange_name}': cannot analyze {symbol} "
                f"realtime: {analysis.error or analysis.status}."
            )

        scored = _score_and_rank([analysis])[0]
        result = RealtimeAnalysis(
            symbol=scored.symbol,
            base=scored.base,
            exchange=self.exchange_name,
            timeframe=self._timeframe,
            price=scored.price,
            volume_24h=scored.volume_24h,
            change_24h=scored.change_24h,
            ema50=scored.ema50,
            ema100=scored.ema100,
            ema200=scored.ema200,
            rsi14=scored.rsi14,
            adx14=scored.adx14,
            atr_pct=scored.atr_pct,
            relative_volume=scored.relative_volume,
            trend_alignment=scored.trend_alignment,
            overall=scored.overall,
            signal=scored.signal,
            recommendation=recommendation_for(scored.signal),
            venue=scored.venue,
            fetched_at=fetched_at,
        )
        with self._lock:
            self._analysis_cache[symbol] = (time.time(), result)
        return result

    def analyze_many(self, symbols: list[str]) -> list[RealtimeAnalysis]:
        """Analyze several symbols in parallel; symbols that fail are
        skipped (their own error is already logged)."""
        if not symbols:
            return []
        results: list[RealtimeAnalysis] = []
        with ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(symbols)),
        ) as pool:
            futures = {
                pool.submit(self.analyze, sym): sym for sym in symbols
            }
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except RealtimeMarketError as exc:
                    _log.warning("Realtime skip %s: %s", futures[fut], exc)
        return results

    # -- Candidate universe (for /signals, /market) ------------------------

    def top_candidates(self, limit: int = DEFAULT_TOP_CANDIDATES, chat_id: str | None = None) -> list[str]:
        """Top *limit* symbols by 24h quote volume on the active exchange,
        filtered to the active quote currency. Fully realtime."""
        self._check_chat_cooldown(chat_id)
        now = time.time()
        with self._lock:
            cached_ts, cached = self._tickers_list_cache
            if cached and now - cached_ts < self._tickers_list_ttl:
                tickers = cached
            else:
                tickers = {}
        if not tickers:
            tickers = self._exchange_call(
                lambda: self._public_exchange_factory(self.exchange_name).fetch_tickers(),
                label="fetch_tickers",
            )
            if not tickers:
                raise RealtimeMarketError(
                    f"Exchange '{self.exchange_name}': failed to fetch "
                    "live tickers. No stale data is shown — please retry "
                    "in a moment."
                )
            with self._lock:
                self._tickers_list_cache = (time.time(), dict(tickers))

        quote = self.quote_currency
        rows: list[tuple[str, float]] = []
        for sym, t in tickers.items():
            if not sym or not sym.endswith(f"/{quote}"):
                continue
            last = t.get("last") or t.get("close") or 0
            vol = t.get("quoteVolume") or 0
            if not last or float(last) <= 0:
                continue
            rows.append((sym, float(vol)))
        if not rows:
            raise RealtimeMarketError(
                f"Exchange '{self.exchange_name}': no '{quote}'-quoted "
                "pairs with live prices found in the ticker feed."
            )
        rows.sort(key=lambda r: r[1], reverse=True)
        return [sym for sym, _ in rows[:limit]]


# ---------------------------------------------------------------------------
#  Command wiring
# ---------------------------------------------------------------------------


def get_realtime_service(ctx: Any) -> RealtimeMarketData:
    """Return the RealtimeMarketData service for a command context.

    Prefers the DI container's persistent instance (cache survives across
    command calls); falls back to building one from ``ctx.config`` so the
    commands also work outside the bot process (CLI, tests).
    """
    services = getattr(ctx, "services", None)
    if services is not None:
        rt = getattr(services, "realtime_market", None)
        if rt is not None:
            return rt
        exchange = getattr(services, "exchange", None)
        if exchange is not None:
            cfg = getattr(services, "config", None)
            timeframe = getattr(cfg, "timeframe", "1h") if cfg is not None else "1h"
            return RealtimeMarketData(exchange, timeframe=timeframe)

    from scripts.exchange_manager import ExchangeManager  # noqa: PLC0415

    cfg = getattr(ctx, "config", None)
    mgr = ExchangeManager(
        active=cfg.exchange,
        quote_currency=getattr(cfg, "quote_currency", "USDT"),
    )
    return RealtimeMarketData(mgr, timeframe=getattr(cfg, "timeframe", "1h"))
