import os
from typing import Any

import pytest

import requests as _requests  # noqa: PLC0415

# ---------------------------------------------------------------------------
#  Real-notification guard
# ---------------------------------------------------------------------------
# Running the test suite must NEVER send real Telegram notifications to the
# production chat. Two real notifier classes read the REAL .env credentials
# (bot/telegram.py TelegramNotifier via bot.config, bot/notifier.py Notifier
# via from_env/from_config) and a few modules instantiate them directly during
# tests — which used to fire genuine BUY OPENED / POSITION CLOSED / STATE
# RESTORED messages into the live chat. The default sandbox below swaps
# ``requests.post`` so ANY attempt to hit api.telegram.org during a test
# FAILS that test loudly. Tests that genuinely exercise remote delivery are
# opt-in only: they must carry ``@pytest.mark.real_notifier`` AND the suite
# must be run with ``ZETBOT_ALLOW_REAL_NOTIFIER=1`` (never set in normal runs).

_ORIGINAL_POST = _requests.post

# os.environ as it exists when conftest is loaded — i.e. BEFORE pytest starts
# collecting test modules. Several modules (bot.config via bot.data, scripts/
# validation.py, …) call load_dotenv() at import time during collection,
# which injects the REAL .env values (e.g. EXCHANGE=indodax) into os.environ.
# _isolate_environ must restore to THIS clean baseline, not to a per-test
# snapshot taken after collection, or the pollution survives every test.
_ENV_BASELINE = os.environ.copy()

# Import bot.config NOW (at conftest load, before collection) so its
# load_dotenv() runs ONCE, then strip the injected .env values back out.
# This is what makes module-level constants (e.g. scripts.risk_manager
# MIN_RR / MAX_ATR_PCT / MIN_STOP_PCT read from os.getenv at import time)
# resolve to their DEFAULT values for every test. If the .env injection
# is allowed to happen during collection instead, modules imported
# afterwards freeze the REAL values (e.g. MIN_RR=3.0 from the bot's .env)
# and risk/validator tests that assume MIN_RR=2.0 fail non-deterministically
# depending on collection order (8 flaky failures in test_risk_sizing.py).
try:
    import bot.config as _bot_config  # noqa: PLC0415
    os.environ.clear()
    os.environ.update(_ENV_BASELINE)
except Exception:
    pass


@pytest.fixture(scope="session", autouse=True)
def _restore_data_dir_after_session():
    """Snapshot ``data/`` before the suite runs and restore it afterwards.

    Many tests deliberately write into ``data/`` (paper_state.json,
    positions.json, live_trade_history.jsonl, .notified_buys, …) to pin
    regression fixes. When the suite runs where the LIVE bot's data dir
    lives, that residue corrupts live accounting: fake ``BTC/USDT``
    ``repro1``/``repro2`` ledger records, phantom OPEN paper positions
    that consume MAX_POSITIONS slots and silently block every new BUY
    (seen 2026-08-15: stale ``paper_state.json`` fixture counted as an
    open position next to ALICE/IDR). Snapshot once at session start,
    restore once at session end.
    """
    import shutil
    import tempfile

    src = os.path.abspath("data")
    backup = tempfile.mkdtemp(prefix="zetbot_data_backup_")
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(backup, "data"), dirs_exist_ok=True)
    yield
    if os.path.isdir(src):
        shutil.rmtree(src, ignore_errors=True)
    saved = os.path.join(backup, "data")
    if os.path.isdir(saved):
        shutil.move(saved, src)
    shutil.rmtree(backup, ignore_errors=True)


def _no_real_telegram_post(*args: Any, **kwargs: Any) -> Any:
    url = str(kwargs.get("url") or (args[0] if args else ""))
    if "api.telegram.org" in url:
        raise AssertionError(
            "Real Telegram send attempted during tests. Every notifier must "
            "be replaced with a mock/disabled stub — mark the test "
            "@pytest.mark.real_notifier AND run with "
            "ZETBOT_ALLOW_REAL_NOTIFIER=1 to deliberately allow it (never "
            "in normal test runs).",
        )
    return _ORIGINAL_POST(*args, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Block every outbound Telegram POST unless explicitly opted in."""
    marker = request.node.get_closest_marker("real_notifier")
    allowed = os.getenv("ZETBOT_ALLOW_REAL_NOTIFIER", "").lower() in ("1", "true", "yes")
    if marker is not None and allowed:
        yield
        return
    # Also cut the CONFIG-driven notifiers at the source (bot/paper_engine,
    # bot/telegram.TelegramNotifier read bot.config.CONFIG at construction)
    # and pin the exchange/quote labels the notifiers embed in messages so
    # fixtures using BTC/USDT pairs never render "indodax … BTC/USDT".
    # NOTE: bot.config calls load_dotenv() at import time, which injects the
    # REAL .env values (e.g. EXCHANGE=indodax) into os.environ. That happens
    # inside an autouse fixture that runs BEFORE _isolate_environ snapshots
    # the env, so the pollution would survive into every later test and flip
    # their exchange resolution. Revert the env right after the import.
    _env_before_import = os.environ.copy()
    import bot.config as _bot_config  # noqa: PLC0415
    os.environ.clear()
    os.environ.update(_env_before_import)
    monkeypatch.setitem(_bot_config.CONFIG, "telegram_enabled", False)
    monkeypatch.setitem(_bot_config.CONFIG, "exchange", "binance")
    monkeypatch.setitem(_bot_config.CONFIG, "quote_currency", "USDT")
    monkeypatch.setattr("requests.post", _no_real_telegram_post)
    yield


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Safety net: no test may leak environment-variable changes to others.

    Restores ``os.environ`` to the pristine pre-collection baseline every
    test — any key added by the test is removed, any key deleted or changed
    is restored. Guards against e.g. a stray ``EXCHANGE=indodax`` leaking
    into later tests and flipping their exchange resolution.
    """
    original = _ENV_BASELINE.copy()
    # pytest manages its own runtime vars (e.g. PYTEST_CURRENT_TEST) around
    # every test and expects them present when it pops them; keep those.
    pytest_vars = {k: v for k, v in os.environ.items() if k.startswith("PYTEST")}
    yield
    os.environ.clear()
    os.environ.update(original)
    os.environ.update(pytest_vars)


@pytest.fixture(autouse=True)
def _clear_provider_caches():
    """Clear the process-wide exchange-instance / markets caches.

    ``scripts.exchange_providers`` shares one CCXT instance per
    (exchange, credentials) and caches ``fetch_markets()`` output for the
    process lifetime. Provider tests that stub ``ccxt`` (or build their
    own providers) must not see instances or market lists cached by
    earlier tests, or their mocked constructors are never called.
    """
    import scripts.exchange_providers as _ep  # noqa: PLC0415
    _ep._EXCHANGE_INSTANCES.clear()
    _ep._MARKETS_CACHE.clear()
    yield
    _ep._EXCHANGE_INSTANCES.clear()
    _ep._MARKETS_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_cwd():
    """Safety net: no test may leak working-directory changes to others.

    Some tests use ``os.chdir(tmp_path)`` directly instead of pytest's
    ``monkeypatch.chdir``. This fixture guarantees the cwd is restored
    after every test so later tests always start from the repo root.
    """
    original_cwd = os.getcwd()
    yield
    try:
        os.chdir(original_cwd)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _no_cached_live_exchange():
    """Safety net: never leak a live exchange client between tests.

    ``bot.data`` keeps a process-global TTL cache of real ccxt clients
    (``_client_cache``) behind ``get_cached_public_exchange()``. A test
    that falls back to the real cache (instead of injecting a fake
    factory) would then reuse a live client built by an earlier test —
    silently reaching the network. Flush the cache around every test so
    no live client survives across tests.
    """
    import bot.data as _bot_data  # noqa: PLC0415
    _bot_data.clear_public_data_cache()
    yield
    _bot_data.clear_public_data_cache()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: marks tests that require live network access (skipped when offline)",
    )
    config.addinivalue_line(
        "markers",
        "exchange(name): live tests that require the named exchange to be reachable",
    )
    config.addinivalue_line(
        "markers",
        "real_notifier: allow real Telegram delivery (requires "
        "ZETBOT_ALLOW_REAL_NOTIFIER=1; never used in normal runs)",
    )


def _network_available() -> bool:
    """Check if Binance API is reachable."""
    try:
        import requests
        resp = requests.get("https://api.binance.com/api/v3/ping", timeout=5)
        resp.raise_for_status()
        return True
    except Exception:
        return False


_reach_cache: dict[str, bool] = {}


def _exchange_reachable(name: str) -> bool:
    """Check a specific exchange's public API, mirroring the ccxt client
    construction in ``bot.data.MarketData``.

    ``@pytest.mark.network`` tests that also carry ``@pytest.mark.exchange(...)``
    are skipped when that exchange is unreachable — e.g. Bybit / Tokocrypto
    are geo-blocked in many regions, so those tests flake against a Binance
    ping alone. Results are cached per exchange for the whole session.
    """
    if name in _reach_cache:
        return _reach_cache[name]
    try:
        import ccxt
        from bot.data import _get_exchange_map

        cls = _get_exchange_map().get(name)
        if cls is None:
            _reach_cache[name] = False
            return False
        ex = cls({"timeout": 5000, "enableRateLimit": False})
        ex.fetch_time()
        _reach_cache[name] = True
    except Exception:
        _reach_cache[name] = False
    return _reach_cache[name]


def pytest_collection_modifyitems(config, items):
    # Live integration tests are opt-in: the default run must be offline
    # deterministic. They run only when explicitly requested with `-m network`.
    markexpr = (config.getoption("markexpr") or "").strip()
    network_requested = bool(markexpr) and "network" in markexpr
    skip_network = not _network_available()
    for item in items:
        if item.get_closest_marker("network") is None:
            continue
        if not network_requested:
            item.add_marker(pytest.mark.skip(
                reason="live network test — run with `-m network` to enable",
            ))
            continue
        if skip_network:
            item.add_marker(pytest.mark.skip(
                reason="binance API unavailable (network/SSL)",
            ))
            continue
        ex = item.get_closest_marker("exchange")
        if ex is not None and ex.args and not _exchange_reachable(ex.args[0]):
            item.add_marker(pytest.mark.skip(
                reason=f"{ex.args[0]} API unavailable (network/SSL)",
            ))


# ===========================================================================
#  Realtime-market test doubles (offline)
# ===========================================================================
# Fake exchange provider + manager + services so the realtime Telegram
# commands (/detail, /pair, /signals, /market) can be exercised fully
# offline. The fakes mimic the surface of BaseProvider / ExchangeManager
# that scripts.realtime_market.py uses.

class FakeProvider:
    """Canned market data provider. Deterministic uptrend OHLCV so the
    scanner indicator path yields a BULLISH, BUY-classified analysis."""

    def __init__(self, exchange: str = "binance", quote: str = "USDT",
                 fail: bool = False, fail_markets: bool = False) -> None:
        self.exchange = exchange
        self.quote = quote
        self.fail = fail
        self.fail_markets = fail_markets
        self.ticker_calls = 0
        self.ohlcv_calls = 0
        self.tickers_calls = 0
        self.markets_calls = 0
        self.base_price: dict[str, float] = {
            "BTC": 60000.0,
            "ETH": 3000.0,
            "SOL": 150.0,
        }

    # -- helpers --------------------------------------------------------

    def _symbol(self, base: str) -> str:
        return f"{base}/{self.quote}"

    def _ticker_row(self, symbol: str, base: str) -> dict:
        return {
            "symbol": symbol,
            "last": self.base_price[base],
            "quoteVolume": 50_000_000.0 if base == "BTC" else 20_000_000.0,
            "percentage": 2.5,
            "high": self.base_price[base] * 1.01,
            "low": self.base_price[base] * 0.99,
        }

    def _candles(self, base: str, limit: int) -> list[list[float]]:
        """250+ candles on a steady uptrend -> BULLISH, score >= 60 -> BUY."""
        n = max(limit, 250)
        step = 0.002
        p0 = self.base_price[base] / (1 + step * (n - 1))
        rows = []
        t0 = 1_700_000_000_000
        for i in range(n):
            close = p0 * (1 + i * step)
            rows.append([
                t0 + i * 3_600_000,
                close * 0.999, close * 1.001, close * 0.998,
                close, 100_000.0 + i,
            ])
        return rows

    # -- provider surface ----------------------------------------------

    def load_markets(self) -> dict:
        self.markets_calls += 1
        if self.fail_markets:
            return {}
        return {
            self._symbol(base): {
                "symbol": self._symbol(base), "base": base,
                "quote": self.quote, "spot": True, "active": True,
            }
            for base in self.base_price
        }

    def get_markets(self) -> list[dict]:
        return list(self.load_markets().values())

    def get_ticker(self, symbol: str) -> dict:
        self.ticker_calls += 1
        if self.fail:
            return {}
        base = symbol.split("/")[0]
        return self._ticker_row(symbol, base)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    limit: int = 200) -> list[list[float]]:
        self.ohlcv_calls += 1
        if self.fail:
            return []
        base = symbol.split("/")[0]
        return self._candles(base, limit)

    def fetch_tickers(self, symbols=None) -> dict:
        self.tickers_calls += 1
        if self.fail:
            return {}
        return {
            self._symbol(base): self._ticker_row(self._symbol(base), base)
            for base in self.base_price
        }


class FakeCCXTExchange:
    """Mimics a ccxt Exchange instance for tests, delegating to FakeProvider."""

    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider
        self.markets: dict[str, Any] = {}

    def load_markets(self) -> dict[str, Any]:
        if not self.markets:
            self.markets = self._provider.load_markets()
        return self.markets

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self._provider.get_ticker(symbol)

    def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        return self._provider.fetch_tickers(symbols)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        return self._provider.fetch_ohlcv(symbol, timeframe, limit)


class FakeExchangeManager:
    """Minimal stand-in for scripts.exchange_manager.ExchangeManager."""

    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider
        self.name = provider.exchange
        self.quote_currency = provider.quote

    def get_provider(self, name: str | None = None) -> FakeProvider:
        return self._provider


class FakeServices:
    """Minimal stand-in for ServiceContainer (exchange + config +
    realtime_market) — the only surface the realtime commands touch."""

    def __init__(self, manager: FakeExchangeManager, config: Any, public_exchange_factory=None) -> None:
        from scripts.realtime_market import RealtimeMarketData  # noqa: PLC0415
        self.exchange = manager
        self.config = config
        provider = manager.get_provider()
        factory = public_exchange_factory or (lambda name: FakeCCXTExchange(provider))
        self.realtime_market = RealtimeMarketData(
            manager,
            timeframe=getattr(config, "timeframe", "1h"),
            _public_exchange_factory=factory,
        )
