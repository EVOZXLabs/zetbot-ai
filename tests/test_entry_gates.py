"""Regression tests for the anti-chase entry gates in decision_engine.

The bot was buying local tops (RSI 78+, 24h pump 12%+, price stretched
45% above EMA200, no trend filter) and every position stop-out'd within
minutes.  These gates must reject such coins BEFORE any scoring can
approve them — generically, for any symbol.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.decision_engine import (
    _gate_reasons,
    ScannerPair,
)


def _pair(**overrides: Any) -> ScannerPair:
    base: dict[str, Any] = {
        "symbol": "TEST/IDR",
        "base": "TEST",
        "price": 1000.0,
        "volume_24h": 1_000_000_000.0,
        "change_24h": 2.0,
        "ema50": 990.0,
        "ema100": 980.0,
        "ema200": 950.0,
        "rsi14": 55.0,
        "adx14": 25.0,
        "atr_pct": 2.5,
        "relative_volume": 1.2,
        "trend_alignment": "BULLISH",
        "trend_score": 70.0,
        "momentum_score": 60.0,
        "volume_score": 65.0,
        "volatility_score": 50.0,
        "liquidity_score": 80.0,
        "overall": 70.0,
        "signal": "GOOD",
        "rank": 1,
    }
    base.update(overrides)
    return ScannerPair(**base)


class TestEntryGates:
    def test_healthy_pullback_passes_all_gates(self) -> None:
        assert _gate_reasons(_pair()) == []

    def test_overbought_rsi_rejected(self) -> None:
        reasons = _gate_reasons(_pair(rsi14=72.0))
        assert any("overbought" in r for r in reasons)

    def test_recent_pump_rejected(self) -> None:
        reasons = _gate_reasons(_pair(change_24h=9.0))
        assert any("pumped" in r for r in reasons)

    def test_overextended_above_ema200_rejected(self) -> None:
        reasons = _gate_reasons(_pair(price=1300.0))  # +37% above EMA200
        assert any("overextended" in r for r in reasons)

    def test_bearish_trend_rejected(self) -> None:
        reasons = _gate_reasons(_pair(trend_alignment="BEARISH"))
        assert any("trend" in r for r in reasons)

    def test_below_ema200_rejected(self) -> None:
        reasons = _gate_reasons(_pair(price=900.0))  # below EMA200 950
        assert any("below EMA200" in r for r in reasons)

    def test_dried_up_volume_rejected(self) -> None:
        reasons = _gate_reasons(_pair(relative_volume=0.2))
        assert any("volume" in r for r in reasons)

    def test_extreme_volatility_rejected(self) -> None:
        reasons = _gate_reasons(_pair(atr_pct=5.5))
        assert any("volatile" in r for r in reasons)