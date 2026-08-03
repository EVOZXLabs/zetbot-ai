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
    import bot.config as _bot_config  # noqa: PLC0415
    monkeypatch.setitem(_bot_config.CONFIG, "telegram_enabled", False)
    monkeypatch.setitem(_bot_config.CONFIG, "exchange", "binance")
    monkeypatch.setitem(_bot_config.CONFIG, "quote_currency", "USDT")
    monkeypatch.setattr("requests.post", _no_real_telegram_post)
    yield


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Safety net: no test may leak environment-variable changes to others.

    Snapshots ``os.environ`` before every test and restores it exactly
    afterwards — any key added by the test is removed, any key deleted or
    changed is restored. Guards against e.g. a stray ``EXCHANGE=indodax``
    leaking into later tests and flipping their exchange resolution.
    """
    original = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(original)


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
