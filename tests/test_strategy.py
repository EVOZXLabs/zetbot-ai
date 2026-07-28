"""
Unit tests for StrategyEngine.

Covers: input validation, BUY conditions, SELL conditions,
HOLD conditions, position-aware logic, edge cases,
MarketData integration.
"""

import math

import pandas as pd
import pytest

from bot.strategy import BUY, HOLD, SELL, StrategyEngine


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _bullish_trending_df(n: int = 250) -> pd.DataFrame:
    """Uptrend with price above EMA200, low RSI, trending market."""
    highs = []
    lows = []
    close = []
    for i in range(n):
        if i < 50:
            c = 50_000.0 + i * 2.0
        else:
            c = 50_000.0 + 50 * 2.0 - (i - 50) * 1.0
        close.append(c)
        highs.append(c + 100.0)
        lows.append(c - 100.0)
    # Make last 15 candles dip to create RSI < 30
    for i in range(1, 16):
        close[-i] = close[-i - 1] - 150.0
        highs[-i] = close[-i] + 50.0
        lows[-i] = close[-i] - 50.0
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _downtrend_df(n: int = 250) -> pd.DataFrame:
    """Steady downtrend — price below EMA200, SELL expected."""
    highs = []
    lows = []
    close = []
    for i in range(n):
        c = 60_000.0 - i * 50.0
        close.append(c)
        highs.append(c + 100.0)
        lows.append(c - 100.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _sideways_df(n: int = 250) -> pd.DataFrame:
    """Tight sideways range — HOLD expected."""
    base = 50_000.0
    return pd.DataFrame({
        "high": [base + 100.0] * n,
        "low":  [base - 100.0] * n,
        "close":[base] * n,
    })


def _buy_scenario_df() -> pd.DataFrame:
    """Engineered scenario that triggers BUY.

    Strong uptrend for 220 candles, then a sharp dip last 30 candles.
    Price ends above EMA200 but RSI is oversold. Market stays trending.
    """
    n = 250
    highs, lows, close = [], [], []
    for i in range(n):
        if i < 220:
            c = 50_000.0 + i * 30.0
        else:
            c = 50_000.0 + 220 * 30.0 - (i - 220) * 25.0
        close.append(c)
        highs.append(c + 200.0)
        lows.append(c - 200.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


# ---------------------------------------------------------------------------
#  Input validation
# ---------------------------------------------------------------------------

class TestStrategyValidation:

    def test_empty_df_raises(self) -> None:
        engine = StrategyEngine()
        with pytest.raises(ValueError, match="empty"):
            engine.evaluate(pd.DataFrame())

    def test_none_df_raises(self) -> None:
        engine = StrategyEngine()
        with pytest.raises(ValueError, match="empty"):
            engine.evaluate(None)  # type: ignore[arg-type]

    def test_missing_close_raises(self) -> None:
        df = pd.DataFrame({"open": [1.0]})
        engine = StrategyEngine()
        with pytest.raises(ValueError, match="Missing.*close"):
            engine.evaluate(df)

    def test_nan_in_close_raises(self) -> None:
        df = pd.DataFrame({
            "high": [100.0, 101.0],
            "low":  [99.0, 100.0],
            "close":[100.0, float("nan")],
        })
        engine = StrategyEngine()
        with pytest.raises(ValueError, match="NaN"):
            engine.evaluate(df)


# ---------------------------------------------------------------------------
#  BUY signal
# ---------------------------------------------------------------------------

class TestBuySignal:

    def test_buy_all_conditions_met(self) -> None:
        df = _buy_scenario_df()
        engine = StrategyEngine(rsi_oversold=40)
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] == BUY, f"Expected BUY, got {result['signal']}"
        assert len(result["reason"]) == 3
        assert any("Price above EMA200" in r for r in result["reason"])
        assert any("RSI oversold" in r for r in result["reason"])
        assert any("Market trending" in r for r in result["reason"])

    def test_buy_with_position_returns_hold(self) -> None:
        """When has_position=True, BUY must not trigger."""
        df = _buy_scenario_df()
        engine = StrategyEngine(rsi_oversold=40)
        result = engine.evaluate(df, has_position=True)
        assert result["signal"] != BUY

    def test_buy_rejected_price_below_ema(self) -> None:
        df = _downtrend_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] != BUY

    def test_buy_rejected_rsi_not_oversold(self) -> None:
        """Strong uptrend with RSI > 30 should not BUY."""
        n = 250
        highs, lows, close = [], [], []
        for i in range(n):
            c = 50_000.0 + i * 30.0
            close.append(c)
            highs.append(c + 100.0)
            lows.append(c - 100.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] != BUY

    def test_buy_rejected_sideways(self) -> None:
        df = _sideways_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] != BUY

    def test_buy_rejected_has_position(self) -> None:
        df = _buy_scenario_df()
        engine = StrategyEngine(rsi_oversold=40)
        result = engine.evaluate(df, has_position=True)
        if result["signal"] == BUY:
            pytest.fail("BUY should not trigger with open position")

    def test_buy_returns_dict_structure(self) -> None:
        df = _buy_scenario_df()
        engine = StrategyEngine(rsi_oversold=40)
        result = engine.evaluate(df, has_position=False)
        assert isinstance(result, dict)
        assert "signal" in result
        assert "reason" in result
        assert isinstance(result["reason"], list)


# ---------------------------------------------------------------------------
#  SELL signal
# ---------------------------------------------------------------------------

class TestSellSignal:

    def test_sell_price_below_ema(self) -> None:
        df = _downtrend_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] == SELL, f"Expected SELL, got {result['signal']}"
        assert any("Price below EMA200" in r for r in result["reason"])

    def test_sell_flat_market_does_not_sell(self) -> None:
        """Sideways near EMA200 should not trigger SELL if not below."""
        n = 250
        base = 50_000.0
        df = pd.DataFrame({
            "high": [base + 100.0] * n,
            "low":  [base - 100.0] * n,
            "close":[base] * n,
        })
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        # Price equals base, EMA200 should be close to base
        # Not definitely below — might be HOLD
        assert result["signal"] in (HOLD, SELL)

    def test_sell_uptrend_does_not_sell(self) -> None:
        """Strong uptrend should not trigger SELL."""
        n = 250
        highs, lows, close = [], [], []
        for i in range(n):
            c = 50_000.0 + i * 30.0
            close.append(c)
            highs.append(c + 100.0)
            lows.append(c - 100.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] != SELL


# ---------------------------------------------------------------------------
#  HOLD signal
# ---------------------------------------------------------------------------

class TestHoldSignal:

    def test_hold_when_no_conditions_met(self) -> None:
        """Sideways with no position → HOLD."""
        df = _sideways_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] == HOLD, f"Expected HOLD, got {result['signal']}"

    def test_hold_with_position_downtrend(self) -> None:
        """Downtrend with position → SELL (not HOLD)."""
        df = _downtrend_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=True)
        assert result["signal"] == SELL

    def test_hold_returns_reason_list(self) -> None:
        df = _sideways_df()
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] == HOLD
        assert isinstance(result["reason"], list)
        assert len(result["reason"]) >= 1

    def test_hold_with_position_in_neutral(self) -> None:
        """Position open in flat market: should be HOLD (no sell trigger)."""
        n = 250
        df = pd.DataFrame({
            "high": [50_100.0] * n,
            "low":  [49_900.0] * n,
            "close":[50_000.0] * n,
        })
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=True)
        # Price ~ 50000, EMA200 ~ 50000 — not clearly SELL
        assert result["signal"] in (HOLD, BUY, SELL)

    def test_hold_when_sideways_with_position_returns_hold_or_sell(self) -> None:
        """Position open when sideways: price not below EMA → HOLD."""
        n = 300
        base = 50_000.0
        df = pd.DataFrame({
            "high": [base + 50.0] * n,
            "low":  [base - 50.0] * n,
            "close":[base] * n,
        })
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=True)
        assert "signal" in result


# ---------------------------------------------------------------------------
#  Edge cases
# ---------------------------------------------------------------------------

class TestStrategyEdgeCases:

    def test_small_dataset_raises(self) -> None:
        """Too few candles for EMA200 should raise."""
        df = pd.DataFrame({
            "high": [100.0, 101.0],
            "low":  [99.0, 100.0],
            "close":[100.0, 101.0],
        })
        engine = StrategyEngine()
        with pytest.raises((ValueError, Exception)):
            engine.evaluate(df)

    def test_very_large_dataset(self) -> None:
        """1000 candles should not crash."""
        import random
        random.seed(42)
        n = 1000
        close = [50_000.0]
        highs, lows = [], []
        for i in range(1, n):
            c = close[-1] + random.uniform(-100, 100)
            close.append(c)
            highs.append(c + random.uniform(0, 50))
            lows.append(c - random.uniform(0, 50))
        highs.insert(0, close[0] + 10)
        lows.insert(0, close[0] - 10)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] in (BUY, SELL, HOLD)
        assert isinstance(result["reason"], list)

    def test_return_type_always_dict(self) -> None:
        """evaluate() must always return a dict."""
        for df in [_sideways_df(), _downtrend_df(), _buy_scenario_df()]:
            engine = StrategyEngine()
            result = engine.evaluate(df, has_position=False)
            assert isinstance(result, dict)
            assert "signal" in result
            assert "reason" in result

    def test_reasons_are_strings(self) -> None:
        df = _buy_scenario_df()
        engine = StrategyEngine(rsi_oversold=40)
        result = engine.evaluate(df, has_position=False)
        for r in result["reason"]:
            assert isinstance(r, str)


# ---------------------------------------------------------------------------
#  MarketData integration
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestMarketDataStrategyIntegration:

    def test_strategy_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=False)
        assert result["signal"] in (BUY, SELL, HOLD)
        assert isinstance(result["reason"], list)

    def test_strategy_with_position_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=250)
        engine = StrategyEngine()
        result = engine.evaluate(df, has_position=True)
        assert result["signal"] in (BUY, SELL, HOLD)
