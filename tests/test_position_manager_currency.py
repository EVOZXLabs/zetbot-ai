"""Tests for quote currency resolution in scripts.position_manager.

Covers the resolution order used by PositionManager._print_summary:
  1. config.quote_currency, if available
  2. QUOTE_CURRENCY environment variable
  3. "USDT" default

This ensures the exchange adapter's configured quote currency (e.g.
IDR for Indodax, USDT for Binance) is picked up automatically instead
of being hardcoded.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from scripts.position_manager import Position, PositionManager


def _make_position(status: str = "OPEN", symbol: str = "BTCUSDT") -> Position:
    """Minimal Position with just enough fields for _print_summary."""
    return Position(
        symbol=symbol,
        entry_price=100.0,
        current_price=110.0,
        position_size_usdt=1_000.0,
        quantity=10.0,
        remaining_pct=100.0,
        remaining_qty=10.0,
        floating_pnl=50.0,
        floating_pnl_pct=5.0,
        realized_pnl=0.0,
        total_pnl=50.0,
        highest_price=110.0,
        lowest_price=95.0,
        stop_loss=90.0,
        current_stop=90.0,
        tp1=120.0,
        tp2=130.0,
        tp3=140.0,
        tp1_hit=False,
        tp2_hit=False,
        tp3_hit=False,
        breakeven_active=False,
        trailing_active=False,
        holding_candles=1,
        holding_hours=1.0,
        entry_time="2026-01-01T00:00:00+00:00",
        status=status,
    )


class TestQuoteCurrencyResolution:
    def test_indodax_config_resolves_to_idr(self) -> None:
        config = SimpleNamespace(quote_currency="IDR")
        manager = PositionManager(config=config)
        manager.positions = [_make_position(symbol="BTCIDR")]

        with patch("builtins.print") as mock_print:
            manager._print_summary(elapsed=0.1)

        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        assert "IDR" in printed
        assert "USDT" not in printed

    def test_binance_config_resolves_to_usdt(self) -> None:
        config = SimpleNamespace(quote_currency="USDT")
        manager = PositionManager(config=config)
        manager.positions = [_make_position()]

        with patch("builtins.print") as mock_print:
            manager._print_summary(elapsed=0.1)

        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        assert "USDT" in printed

    def test_falls_back_to_env_when_no_config(self) -> None:
        manager = PositionManager(config=None)
        manager.positions = [_make_position(symbol="BTCIDR")]

        with patch.dict(os.environ, {"QUOTE_CURRENCY": "IDR"}, clear=False):
            with patch("builtins.print") as mock_print:
                manager._print_summary(elapsed=0.1)

        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        assert "IDR" in printed

    def test_falls_back_to_usdt_default(self) -> None:
        manager = PositionManager(config=None)
        manager.positions = [_make_position()]

        env = dict(os.environ)
        env.pop("QUOTE_CURRENCY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("builtins.print") as mock_print:
                manager._print_summary(elapsed=0.1)

        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        assert "USDT" in printed
