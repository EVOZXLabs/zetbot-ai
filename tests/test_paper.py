"""
Unit tests for PaperTrader.

Covers: open position, duplicate prevention, has_position, reset,
position values, SL/TP calculation, position size, config integration,
StrategyEngine integration.
"""

from datetime import datetime, timezone

import pytest

from bot.paper import PaperTrader


# ---------------------------------------------------------------------------
#  open_position
# ---------------------------------------------------------------------------

class TestOpenPosition:

    def test_open_position_returns_position_dict(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Price above EMA200", "RSI oversold"],
        )
        assert isinstance(result, dict)
        assert result["status"] == "OPEN"
        assert result["symbol"] == "BTC/USDT"
        assert result["timeframe"] == "1h"
        assert result["entry_price"] == 50_000.0

    def test_open_position_returns_none_when_already_open(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result1 = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result1 is not None

        result2 = trader.open_position(
            entry_price=51_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result2 is None, "Duplicate BUY must return None"

    def test_open_position_sets_entry_time(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert isinstance(result["entry_time"], datetime)

    def test_open_position_entry_time_is_utc(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="ETH/USDT",
            timeframe="4h",
            reasons=["Test"],
        )
        tz = result["entry_time"].tzinfo
        assert tz is not None
        assert tz.utcoffset(None) == timezone.utc.utcoffset(None), (
            "entry_time must be timezone-aware UTC"
        )

    def test_open_position_stores_quantity(self) -> None:
        """10% of 10_000 = 1000 USDT. At 50000: qty = 0.02"""
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result["quantity"] == pytest.approx(0.02, rel=1e-6)
        assert result["balance_before"] == 10_000.0


# ---------------------------------------------------------------------------
#  Duplicate BUY prevention
# ---------------------------------------------------------------------------

class TestDuplicateBuyPrevention:

    def test_second_open_returns_none(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        second = trader.open_position(
            entry_price=51_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert second is None

    def test_after_reset_can_open_again(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        first = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert first is not None
        trader.reset()
        second = trader.open_position(
            entry_price=52_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert second is not None

    def test_has_position_blocks_buy(self) -> None:
        """Simulate the main.py flow: check has_position before BUY."""
        trader = PaperTrader(initial_balance=10_000.0)
        assert not trader.has_position()

        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert trader.has_position()

        result = trader.open_position(
            entry_price=51_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result is None


# ---------------------------------------------------------------------------
#  has_position
# ---------------------------------------------------------------------------

class TestHasPosition:

    def test_has_position_false_initially(self) -> None:
        trader = PaperTrader()
        assert not trader.has_position()

    def test_has_position_true_after_open(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert trader.has_position()

    def test_has_position_false_after_reset(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.reset()
        assert not trader.has_position()


# ---------------------------------------------------------------------------
#  reset
# ---------------------------------------------------------------------------

class TestReset:

    def test_reset_clears_position(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert trader.has_position()
        trader.reset()
        assert trader.current_position() is None

    def test_reset_allows_new_position(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.reset()
        position = trader.open_position(
            entry_price=55_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert position is not None

    def test_reset_idempotent(self) -> None:
        trader = PaperTrader()
        trader.reset()
        assert not trader.has_position()
        assert trader.current_position() is None


# ---------------------------------------------------------------------------
#  Position values
# ---------------------------------------------------------------------------

class TestPositionValues:

    def test_position_has_all_required_keys(self) -> None:
        required_keys = {
            "entry_time", "entry_price", "quantity", "balance_before",
            "position_size_percent", "stop_loss_price", "take_profit_price",
            "status", "symbol", "timeframe",
        }
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        missing = required_keys - set(result.keys())
        assert not missing, f"Position missing keys: {missing}"

    def test_position_values_are_correct_types(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert isinstance(result["entry_time"], datetime)
        assert isinstance(result["entry_price"], float)
        assert isinstance(result["quantity"], float)
        assert isinstance(result["balance_before"], float)
        assert isinstance(result["position_size_percent"], float)
        assert isinstance(result["stop_loss_price"], float)
        assert isinstance(result["take_profit_price"], float)
        assert isinstance(result["status"], str)
        assert isinstance(result["symbol"], str)
        assert isinstance(result["timeframe"], str)

    def test_current_position_copies_result(self) -> None:
        """current_position() must not return the internal dict."""
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        pos1 = trader.current_position()
        pos2 = trader.current_position()
        assert pos1 is not None
        assert pos2 is not None
        assert pos1 is not pos2

    def test_current_position_none_when_no_position(self) -> None:
        trader = PaperTrader()
        assert trader.current_position() is None

    def test_current_position_after_reset_is_none(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.reset()
        assert trader.current_position() is None


# ---------------------------------------------------------------------------
#  SL / TP calculation
# ---------------------------------------------------------------------------

class TestRiskCalculation:

    def test_stop_loss_default(self) -> None:
        """Default SL = 1.5%. Price 50000 → 49250."""
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result["stop_loss_price"] == 50_000.0 * (1.0 - 1.5 / 100.0)

    def test_take_profit_default(self) -> None:
        """Default TP = 2.5%. Price 50000 → 51250."""
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result["take_profit_price"] == 50_000.0 * (1.0 + 2.5 / 100.0)

    def test_stop_loss_at_high_price(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=100_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        expected_sl = 100_000.0 * (1.0 - 1.5 / 100.0)
        assert result["stop_loss_price"] == pytest.approx(expected_sl)

    def test_take_profit_at_low_price(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=1_000.0,
            symbol="XRP/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        expected_tp = 1_000.0 * (1.0 + 2.5 / 100.0)
        assert result["take_profit_price"] == pytest.approx(expected_tp)

    def test_sl_always_below_entry(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        for price in [10.0, 100.0, 1_000.0, 50_000.0, 1_000_000.0]:
            trader.reset()
            result = trader.open_position(
                entry_price=price,
                symbol="TEST/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            assert result["stop_loss_price"] < price

    def test_tp_always_above_entry(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        for price in [10.0, 100.0, 1_000.0, 50_000.0, 1_000_000.0]:
            trader.reset()
            result = trader.open_position(
                entry_price=price,
                symbol="TEST/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            assert result["take_profit_price"] > price


# ---------------------------------------------------------------------------
#  Position size calculation
# ---------------------------------------------------------------------------

class TestPositionSize:

    def test_default_size_10_percent(self) -> None:
        """10% of 10_000 = 1000 USDT at entry."""
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        value = result["quantity"] * result["entry_price"]
        expected = 10_000.0 * (10.0 / 100.0)
        assert value == pytest.approx(expected, rel=1e-3)
        assert result["position_size_percent"] == 10.0

    def test_position_size_percent_stored(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert result["position_size_percent"] == 10.0

    def test_quantity_computed_correctly(self) -> None:
        """At entry 50000, 10% of 10000 = 1000 USDT → 0.02 BTC."""
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        expected_qty = (10_000.0 * 0.1) / 50_000.0
        assert result["quantity"] == pytest.approx(expected_qty, rel=1e-6)

    def test_position_value_scales_with_balance(self) -> None:
        trader = PaperTrader(initial_balance=100_000.0)
        result = trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        value = result["quantity"] * result["entry_price"]
        assert value == pytest.approx(10_000.0, rel=1e-3)

    def test_larger_entry_reduces_quantity(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.open_position(
            entry_price=100_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        value = result["quantity"] * result["entry_price"]
        assert value == pytest.approx(1_000.0, rel=1e-3)
        assert result["quantity"] == pytest.approx(0.01, rel=1e-6)


# ---------------------------------------------------------------------------
#  Config integration
# ---------------------------------------------------------------------------

class TestConfigIntegration:

    def test_uses_config_position_size(self) -> None:
        """When CONFIG.position_size = 20, use 20%."""
        import bot.config as cfg
        original = cfg.CONFIG.get("position_size")
        cfg.CONFIG["position_size"] = 20
        try:
            trader = PaperTrader(initial_balance=10_000.0)
            result = trader.open_position(
                entry_price=50_000.0,
                symbol="BTC/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            value = result["quantity"] * result["entry_price"]
            assert value == pytest.approx(2_000.0, rel=1e-3)
            assert result["position_size_percent"] == 20.0
        finally:
            cfg.CONFIG["position_size"] = original

    def test_uses_config_stop_loss(self) -> None:
        import bot.config as cfg
        original = cfg.CONFIG.get("stop_loss")
        cfg.CONFIG["stop_loss"] = 3.0
        try:
            trader = PaperTrader(initial_balance=10_000.0)
            result = trader.open_position(
                entry_price=50_000.0,
                symbol="BTC/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            expected_sl = 50_000.0 * (1.0 - 3.0 / 100.0)
            assert result["stop_loss_price"] == pytest.approx(expected_sl)
        finally:
            cfg.CONFIG["stop_loss"] = original

    def test_uses_config_take_profit(self) -> None:
        import bot.config as cfg
        original = cfg.CONFIG.get("take_profit")
        cfg.CONFIG["take_profit"] = 5.0
        try:
            trader = PaperTrader(initial_balance=10_000.0)
            result = trader.open_position(
                entry_price=50_000.0,
                symbol="BTC/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            expected_tp = 50_000.0 * (1.0 + 5.0 / 100.0)
            assert result["take_profit_price"] == pytest.approx(expected_tp)
        finally:
            cfg.CONFIG["take_profit"] = original


# ---------------------------------------------------------------------------
#  StrategyEngine integration
# ---------------------------------------------------------------------------

class TestStrategyIntegration:

    def test_buy_signal_opens_position(self) -> None:
        """Simulate end-to-end: strategy → paper open."""
        import pandas as pd
        from bot.strategy import BUY, StrategyEngine

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
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})

        engine = StrategyEngine(rsi_oversold=40)
        trader = PaperTrader(initial_balance=10_000.0)

        result = engine.evaluate(df, has_position=trader.has_position())
        assert result["signal"] == BUY

        pos = trader.open_position(
            entry_price=float(df["close"].iloc[-1]),
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=result["reason"],
        )
        assert pos is not None
        assert pos["status"] == "OPEN"

    def test_buy_ignored_when_position_exists(self) -> None:
        """Opening position, then BUY → ignored."""
        import pandas as pd
        from bot.strategy import BUY, StrategyEngine

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
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})

        engine = StrategyEngine(rsi_oversold=40)
        trader = PaperTrader(initial_balance=10_000.0)

        result1 = engine.evaluate(df, has_position=trader.has_position())
        trader.open_position(
            entry_price=float(df["close"].iloc[-1]),
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=result1["reason"],
        )

        result2 = engine.evaluate(df, has_position=trader.has_position())
        assert result2["signal"] != BUY

    def test_no_position_when_signal_hold(self) -> None:
        """HOLD signal → no position opened."""
        n = 250
        base = 50_000.0
        import pandas as pd
        df = pd.DataFrame({
            "high": [base + 100.0] * n,
            "low":  [base - 100.0] * n,
            "close":[base] * n,
        })
        from bot.strategy import HOLD, StrategyEngine
        engine = StrategyEngine()
        trader = PaperTrader(initial_balance=10_000.0)

        result = engine.evaluate(df, has_position=trader.has_position())
        assert result["signal"] == HOLD
        assert not trader.has_position()
