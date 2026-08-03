import os

import pytest


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
