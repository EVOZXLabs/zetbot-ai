"""
Exchange Connection Test for ZetBot AI.

Tests API connectivity, account access, and server time.
Never places orders.
"""

import sys
import time
from typing import Any


def run_exchange_test() -> str:
    """Test exchange API connection. Returns a formatted report string."""
    try:
        from scripts.app_config import load_config
        config = load_config()
    except Exception as exc:
        return f"Failed to load config: {exc}"

    lines = ["=== Exchange Connection Test ===\n"]
    lines.append(f"  Exchange: {config.exchange}")

    import ccxt
    from scripts.exchange_providers import get_provider_class, list_supported_exchanges

    try:
        ccxt_name = get_provider_class(config.exchange)().ccxt_name
    except KeyError:
        lines.append(
            f"  Status:   FAIL — unknown exchange '{config.exchange}' "
            f"(supported: {', '.join(list_supported_exchanges())})"
        )
        return "\n".join(lines)

    exchange_class = getattr(ccxt, ccxt_name, None)
    if exchange_class is None:
        lines.append(f"  Status:   FAIL — CCXT has no exchange '{ccxt_name}'")
        return "\n".join(lines)

    exchange = exchange_class({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
        "timeout": 15000,
    })

    try:
        status = exchange.fetch_status() if hasattr(exchange, "fetch_status") else {}
        status_str = status.get("status", "ok") if status else "ok"
        lines.append(f"  API:      {status_str}")
    except Exception as exc:
        lines.append(f"  API:      FAIL — {exc}")
        lines.append(f"  (No API key required for public status)")
        status_str = "unknown"

    try:
        server_time = exchange.fetch_time() if hasattr(exchange, "fetch_time") else 0
        if server_time:
            local_ms = int(time.time() * 1000)
            delay = abs(local_ms - server_time)
            lines.append(f"  Latency:  {delay}ms")
        else:
            lines.append(f"  Latency:  N/A")
    except Exception:
        lines.append(f"  Latency:  N/A")

    has_api_key = bool(config.exchange and hasattr(config, 'api_key') and getattr(config, 'api_key', None))

    if has_api_key:
        try:
            exchanges = exchange_class({
                "apiKey": getattr(config, 'api_key', ''),
                "secret": getattr(config, 'api_secret', ''),
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
                "timeout": 15000,
            })
            balance = exchanges.fetch_balance()
            if balance.get("info"):
                lines.append(f"  Account:  accessible")
            total_btc = balance.get("total", {}).get("BTC", 0)
            lines.append(f"  BTC:      {total_btc:.8f}")
            symbols = exchange.symbols if hasattr(exchange, "symbols") else []
            has_spot = "SPOT" in str(exchange.options.get("defaultType", "")).upper()
            lines.append(f"  Spot:     {'enabled' if has_spot else 'available'}")
        except Exception as exc:
            lines.append(f"  Account:  FAIL — {exc}")
    else:
        lines.append(f"  Account:  skipped (no API key configured)")

    lines.append(f"\n  No orders were placed.")

    return "\n".join(lines)
