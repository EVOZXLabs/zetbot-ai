import pytest

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: marks tests that require network access (skipped when offline)",
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

def pytest_collection_modifyitems(config, items):
    skip_network = not _network_available()
    if skip_network:
        skip_marker = pytest.mark.skip(reason="binance API unavailable (network/SSL)")
        for item in items:
            if item.get_closest_marker("network"):
                item.add_marker(skip_marker)
