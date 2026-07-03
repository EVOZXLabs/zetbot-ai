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


# ---------------------------------------------------------------------------
#  close_position – Take Profit
# ---------------------------------------------------------------------------

class TestCloseTakeProfit:

    def _open_and_close(self, entry: float, exit_: float) -> dict:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=entry,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        result = trader.close_position(exit_, "Take Profit")
        assert result is not None
        return result

    def test_tp_has_required_keys(self) -> None:
        trade = self._open_and_close(50_000.0, 51_250.0)
        required = {
            "entry_time", "exit_time", "entry_price", "exit_price",
            "quantity", "position_size_percent", "stop_loss_price",
            "take_profit_price", "gross_pnl", "net_pnl", "pnl_pct",
            "holding_time", "exit_reason", "balance_after",
            "symbol", "timeframe",
        }
        missing = required - set(trade.keys())
        assert not missing, f"Closed trade missing keys: {missing}"

    def test_tp_profit_correct(self) -> None:
        trade = self._open_and_close(50_000.0, 52_000.0)
        expected_pnl = (52_000.0 - 50_000.0) * 0.02
        assert trade["gross_pnl"] == pytest.approx(expected_pnl)
        assert trade["net_pnl"] == pytest.approx(expected_pnl)

    def test_tp_pnl_pct_correct(self) -> None:
        trade = self._open_and_close(50_000.0, 52_000.0)
        expected_pct = ((52_000.0 / 50_000.0) - 1.0) * 100.0
        assert trade["pnl_pct"] == pytest.approx(expected_pct)

    def test_tp_balance_updated(self) -> None:
        trade = self._open_and_close(50_000.0, 52_000.0)
        expected_balance = 10_000.0 + (52_000.0 - 50_000.0) * 0.02
        assert trade["balance_after"] == pytest.approx(expected_balance)

    def test_tp_stores_exit_reason(self) -> None:
        trade = self._open_and_close(50_000.0, 51_250.0)
        assert trade["exit_reason"] == "Take Profit"

    def test_tp_clears_position(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert trader.has_position()
        trader.close_position(51_250.0, "Take Profit")
        assert not trader.has_position()

    def test_tp_is_profitable(self) -> None:
        trade = self._open_and_close(50_000.0, 51_250.0)
        assert trade["net_pnl"] > 0
        assert trade["gross_pnl"] > 0

    def test_tp_entry_time_is_datetime(self) -> None:
        trade = self._open_and_close(50_000.0, 51_250.0)
        assert isinstance(trade["entry_time"], datetime)

    def test_tp_exit_time_is_datetime(self) -> None:
        trade = self._open_and_close(50_000.0, 51_250.0)
        assert isinstance(trade["exit_time"], datetime)

    def test_tp_holding_time_is_timedelta(self) -> None:
        from datetime import timedelta
        trade = self._open_and_close(50_000.0, 51_250.0)
        assert isinstance(trade["holding_time"], timedelta)


# ---------------------------------------------------------------------------
#  close_position – Stop Loss
# ---------------------------------------------------------------------------

class TestCloseStopLoss:

    def test_sl_exit_reason(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(48_000.0, "Stop Loss")
        assert trade is not None
        assert trade["exit_reason"] == "Stop Loss"

    def test_sl_net_pnl_negative(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(49_000.0, "Stop Loss")
        assert trade["net_pnl"] < 0
        assert trade["gross_pnl"] < 0

    def test_sl_pnl_pct_negative(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(49_000.0, "Stop Loss")
        assert trade["pnl_pct"] < 0

    def test_sl_balance_decreases(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(49_000.0, "Stop Loss")
        assert trade is not None
        assert trade["balance_after"] < 10_000.0

    def test_sl_clears_position(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(49_000.0, "Stop Loss")
        assert not trader.has_position()


# ---------------------------------------------------------------------------
#  close_position – Strategy Exit / Manual Close
# ---------------------------------------------------------------------------

class TestCloseStrategyExit:

    def test_strategy_exit_reason(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(51_000.0, "Strategy Exit")
        assert trade is not None
        assert trade["exit_reason"] == "Strategy Exit"

    def test_manual_close_reason(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(50_500.0, "Manual Close")
        assert trade is not None
        assert trade["exit_reason"] == "Manual Close"

    def test_zero_pnl_when_exit_equals_entry(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(50_000.0, "Manual Close")
        assert trade["net_pnl"] == pytest.approx(0.0)
        assert trade["pnl_pct"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
#  close_position – invalid / edge cases
# ---------------------------------------------------------------------------

class TestCloseInvalid:

    def test_close_no_position_returns_none(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        result = trader.close_position(50_000.0, "Manual Close")
        assert result is None

    def test_close_after_close_returns_none(self) -> None:
        """Double close must return None on second call."""
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        first = trader.close_position(52_000.0, "Take Profit")
        assert first is not None
        second = trader.close_position(53_000.0, "Take Profit")
        assert second is None

    def test_close_zero_price_raises(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        with pytest.raises(ValueError, match="positive"):
            trader.close_position(0.0, "Manual Close")

    def test_close_negative_price_raises(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        with pytest.raises(ValueError, match="positive"):
            trader.close_position(-100.0, "Manual Close")


# ---------------------------------------------------------------------------
#  Trade history
# ---------------------------------------------------------------------------

class TestTradeHistory:

    def test_history_empty_initially(self) -> None:
        trader = PaperTrader()
        assert trader.trade_history() == []

    def test_history_contains_closed_trade(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        history = trader.trade_history()
        assert len(history) == 1
        assert history[0]["exit_reason"] == "Take Profit"

    def test_history_multiple_trades(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        trader.open_position(
            entry_price=51_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(53_000.0, "Take Profit")
        assert len(trader.trade_history()) == 2

    def test_history_ordered_oldest_first(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        trader.open_position(
            entry_price=55_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(57_000.0, "Take Profit")
        hist = trader.trade_history()
        assert hist[0]["entry_price"] == 50_000.0
        assert hist[1]["entry_price"] == 55_000.0

    def test_history_not_mutable(self) -> None:
        """trade_history() must return a copy."""
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        h1 = trader.trade_history()
        h2 = trader.trade_history()
        assert h1 is not h2

    def test_history_after_reset_empty(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        trader.reset()
        assert trader.trade_history() == []


# ---------------------------------------------------------------------------
#  last_trade
# ---------------------------------------------------------------------------

class TestLastTrade:

    def test_last_trade_none_initially(self) -> None:
        trader = PaperTrader()
        assert trader.last_trade() is None

    def test_last_trade_returns_most_recent(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(51_000.0, "Strategy Exit")
        trader.open_position(
            entry_price=52_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(54_000.0, "Take Profit")
        last = trader.last_trade()
        assert last is not None
        assert last["entry_price"] == 52_000.0
        assert last["exit_price"] == 54_000.0

    def test_last_trade_is_copy(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(51_000.0, "Strategy Exit")
        l1 = trader.last_trade()
        l2 = trader.last_trade()
        assert l1 is not None and l2 is not None
        assert l1 is not l2

    def test_last_trade_after_reset_none(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        trader.reset()
        assert trader.last_trade() is None


# ---------------------------------------------------------------------------
#  total_profit / win_count / loss_count
# ---------------------------------------------------------------------------

class TestProfitStats:

    def test_total_profit_zero_initially(self) -> None:
        trader = PaperTrader()
        assert trader.total_profit() == 0.0

    def test_total_profit_one_trade(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        expected = (52_000.0 - 50_000.0) * 0.02
        assert trader.total_profit() == pytest.approx(expected)

    def test_total_profit_multiple_trades(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        t1 = trader.close_position(52_000.0, "Take Profit")
        pnl1 = t1["net_pnl"]
        trader.open_position(
            entry_price=55_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        t2 = trader.close_position(53_000.0, "Stop Loss")
        pnl2 = t2["net_pnl"]
        assert trader.total_profit() == pytest.approx(pnl1 + pnl2)

    def test_win_count(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")  # win
        trader.open_position(
            entry_price=55_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(53_000.0, "Stop Loss")   # loss
        assert trader.win_count() == 1
        assert trader.loss_count() == 1

    def test_win_count_zero(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        assert trader.win_count() == 0
        assert trader.loss_count() == 0

    def test_loss_on_zero_pnl(self) -> None:
        """Zero PnL counts as loss (not win)."""
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(50_000.0, "Manual Close")
        assert trader.win_count() == 0
        assert trader.loss_count() == 1

    def test_all_wins(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        for _ in range(3):
            trader.open_position(
                entry_price=50_000.0,
                symbol="BTC/USDT",
                timeframe="1h",
                reasons=["Test"],
            )
            trader.close_position(52_000.0, "Take Profit")
        assert trader.win_count() == 3
        assert trader.loss_count() == 0


# ---------------------------------------------------------------------------
#  Balance update
# ---------------------------------------------------------------------------

class TestBalanceUpdate:

    def test_balance_increases_on_profit(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(52_000.0, "Take Profit")
        assert trade["balance_after"] > 10_000.0
        assert trade["balance_after"] == pytest.approx(trader._balance)

    def test_balance_decreases_on_loss(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(48_000.0, "Stop Loss")
        assert trade["balance_after"] < 10_000.0

    def test_balance_persists_across_trades(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trader.close_position(52_000.0, "Take Profit")
        bal1 = trader._balance
        trader.open_position(
            entry_price=51_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        # position_value uses updated balance
        pos = trader.current_position()
        assert pos is not None
        expected_qty = (bal1 * 0.1) / 51_000.0
        assert pos["quantity"] == pytest.approx(expected_qty)


# ---------------------------------------------------------------------------
#  close_position symbol / timeframe stored
# ---------------------------------------------------------------------------

class TestCloseMetadata:

    def test_symbol_preserved(self) -> None:
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="ETH/USDT",
            timeframe="4h",
            reasons=["Test"],
        )
        trade = trader.close_position(52_000.0, "Take Profit")
        assert trade is not None
        assert trade["symbol"] == "ETH/USDT"
        assert trade["timeframe"] == "4h"

    def test_all_fields_are_correct_types(self) -> None:
        from datetime import datetime, timedelta
        trader = PaperTrader(initial_balance=10_000.0)
        trader.open_position(
            entry_price=50_000.0,
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        trade = trader.close_position(52_000.0, "Take Profit")
        assert trade is not None
        assert isinstance(trade["entry_time"], datetime)
        assert isinstance(trade["exit_time"], datetime)
        assert isinstance(trade["holding_time"], timedelta)
        assert isinstance(trade["entry_price"], float)
        assert isinstance(trade["exit_price"], float)
        assert isinstance(trade["quantity"], float)
        assert isinstance(trade["gross_pnl"], float)
        assert isinstance(trade["net_pnl"], float)
        assert isinstance(trade["pnl_pct"], float)
        assert isinstance(trade["exit_reason"], str)
        assert isinstance(trade["balance_after"], float)


# ---------------------------------------------------------------------------
#  End-to-end StrategyEngine + close
# ---------------------------------------------------------------------------

class TestStrategySellIntegration:

    def test_sell_signal_closes_position(self) -> None:
        """SELL signal with position open → close with Strategy Exit."""
        import pandas as pd
        from bot.strategy import HOLD, SELL, StrategyEngine

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
        trader.open_position(
            entry_price=float(df["close"].iloc[-1]),
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=result["reason"],
        )
        assert trader.has_position()

        df2 = _downtrend_df()
        result2 = engine.evaluate(df2, has_position=trader.has_position())
        pos = trader.current_position()
        assert pos is not None
        price = float(df2["close"].iloc[-1])
        if result2["signal"] == SELL:
            trade = trader.close_position(price, "Strategy Exit")
            assert trade is not None
            assert trade["exit_reason"] == "Strategy Exit"
            assert not trader.has_position()

    def test_hold_with_position_does_not_close(self) -> None:
        """When signal is HOLD but position exists — leave open."""
        import pandas as pd
        from bot.strategy import HOLD, StrategyEngine

        df = _uptrend_flat_df()
        engine = StrategyEngine()
        trader = PaperTrader(initial_balance=10_000.0)

        trader.open_position(
            entry_price=float(df["close"].iloc[-1]),
            symbol="BTC/USDT",
            timeframe="1h",
            reasons=["Test"],
        )
        assert trader.has_position()

        result = engine.evaluate(df, has_position=trader.has_position())
        assert result["signal"] == HOLD
        assert trader.has_position()


# ---------------------------------------------------------------------------
#  Helpers (shared)
# ---------------------------------------------------------------------------

def _downtrend_df(n: int = 250) -> pd.DataFrame:
    import pandas as pd
    highs = []
    lows = []
    close = []
    for i in range(n):
        c = 60_000.0 - i * 50.0
        close.append(c)
        highs.append(c + 100.0)
        lows.append(c - 100.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _uptrend_flat_df(n: int = 250) -> pd.DataFrame:
    """Uptrend that ends flat — evaluates to HOLD."""
    import pandas as pd
    base = 50_000.0
    return pd.DataFrame({
        "high": [base + 100.0] * n,
        "low":  [base - 100.0] * n,
        "close":[base] * n,
    })
