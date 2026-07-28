"""
Unit tests for PaperTradingEngine.

Covers: initialisation, BUY workflow, SELL workflow, no-signal,
position monitoring, TP/SL exit, trade history, statistics,
balance updates, duplicate BUY prevention, state transitions.
"""

import math
from datetime import timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bot.paper_engine import (
    ANALYZE,
    BUY_SIGNAL,
    EVALUATE,
    IDLE,
    MONITOR,
    SELL_SIGNAL,
    PaperTradingEngine,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _uptrend_buy_df() -> pd.DataFrame:
    """Strong uptrend with a dip — triggers BUY signal."""
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


def _downtrend_df() -> pd.DataFrame:
    """Steady downtrend — triggers SELL."""
    n = 250
    close = [60_000.0 - i * 50.0 for i in range(n)]
    highs = [c + 100.0 for c in close]
    lows  = [c - 100.0 for c in close]
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _strategy_exit_df(above: float = 49_300.0) -> pd.DataFrame:
    """DataFrame where price is below EMA200 but above SL level.

    Used to test Strategy Exit without hitting Stop Loss first.

    Args:
        above: The close price (must be above SL, below EMA200).
    """
    n = 250
    base = 50_000.0
    return pd.DataFrame({
        "high": [base + 100.0] * n,
        "low":  [base - 100.0] * n,
        "close":[above] * n,
    })


def _sideways_df() -> pd.DataFrame:
    """Flat market — no signal."""
    base = 50_000.0
    n = 250
    return pd.DataFrame({
        "high": [base + 100.0] * n,
        "low":  [base - 100.0] * n,
        "close":[base] * n,
    })


def _tp_hit_df(entry: float = 50_000.0) -> pd.DataFrame:
    """Price above TP level."""
    return pd.DataFrame({
        "high": [entry * 1.05] * 250,
        "low":  [entry * 0.95] * 250,
        "close":[entry * 1.04] * 250,
    })


def _sl_hit_df(entry: float = 50_000.0) -> pd.DataFrame:
    """Price below SL level."""
    return pd.DataFrame({
        "high": [entry * 1.01] * 250,
        "low":  [entry * 0.96] * 250,
        "close":[entry * 0.98] * 250,
    })


# ---------------------------------------------------------------------------
#  Initialisation
# ---------------------------------------------------------------------------

class TestEngineInit:

    def test_engine_initialises(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        status = engine.status()
        assert status["state"] == IDLE
        assert status["balance"] == 10_000.0
        assert status["position"] is None

    def test_default_balance(self) -> None:
        engine = PaperTradingEngine()
        assert engine.current_balance() == 10_000.0

    def test_custom_balance(self) -> None:
        engine = PaperTradingEngine(initial_balance=50_000.0)
        assert engine.current_balance() == 50_000.0

    def test_initial_statistics_empty(self) -> None:
        engine = PaperTradingEngine()
        stats = engine.statistics()
        assert stats["total_trades"] == 0
        assert stats["total_profit"] == 0.0
        assert stats["win_rate"] == 0.0


# ---------------------------------------------------------------------------
#  BUY workflow
# ---------------------------------------------------------------------------

class TestBuyWorkflow:

    def test_buy_opens_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = _uptrend_buy_df()
        result = engine.run_once(df=df)
        assert result["position"] is not None
        assert result["position"]["status"] == "OPEN"
        assert result["signal"] is not None
        assert result["signal"]["signal"] == "BUY"

    def test_buy_returns_correct_metadata(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = _uptrend_buy_df()
        result = engine.run_once(df=df)
        assert "state" in result
        assert "signal" in result
        assert "trade" in result
        assert "position" in result
        assert "price" in result
        assert "market_state" in result

    def test_buy_updates_last_signal(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = _uptrend_buy_df()
        engine.run_once(df=df)
        status = engine.status()
        assert status["last_signal"] is not None
        assert status["last_signal"]["signal"] == "BUY"

    def test_buy_updates_market_state(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = _uptrend_buy_df()
        engine.run_once(df=df)
        status = engine.status()
        assert status["market_state"] in ("TRENDING", "SIDEWAYS")

    def test_buy_ends_in_idle(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = _uptrend_buy_df()
        result = engine.run_once(df=df)
        assert result["state"] == IDLE
        assert engine.status()["state"] == IDLE


# ---------------------------------------------------------------------------
#  SELL workflow (strategy exit)
# ---------------------------------------------------------------------------

class TestSellWorkflow:

    def test_sell_closes_position(self) -> None:
        import bot.config as cfg
        from bot.indicators import IndicatorEngine
        orig_sl = cfg.CONFIG.get("stop_loss")
        cfg.CONFIG["stop_loss"] = 10.0
        # This test targets the Strategy-Exit code path, not stop
        # sizing, so it needs the wide 10% fixed stop above rather than
        # the tighter ATR-based stop that's now used by default when
        # candle data is available. Force the ATR path unavailable so
        # PaperTrader falls back to the fixed % configured above.
        orig_atr = IndicatorEngine.atr
        IndicatorEngine.atr = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("no atr in test"))
        )
        try:
            engine = PaperTradingEngine(initial_balance=10_000.0)
            engine.run_once(df=_uptrend_buy_df())
            pos = engine.current_position()
            assert pos is not None, "BUY should open a position"

            entry = pos["entry_price"]
            sl = pos["stop_loss_price"]
            # SL is at 10% below entry, so sl ≈ entry * 0.9
            # Strategy Exit df: close declines from 60000 to below entry EMA200
            # but stays above sl
            n = 250
            close = [60_000.0 - i * 36.0 for i in range(n)]
            exit_df = pd.DataFrame({
                "high": [c + 100.0 for c in close],
                "low":  [c - 100.0 for c in close],
                "close": close,
            })
            result = engine.run_once(df=exit_df)
            assert result["trade"] is not None, f"No trade: exit_price={close[-1]:.1f} sl={sl:.1f}"
            assert result["trade"]["exit_reason"] == "Strategy Exit", (
                f"Got {result['trade']['exit_reason']} "
                f"exit={close[-1]:.1f} sl={sl:.1f}"
            )
            assert result["position"] is None
        finally:
            cfg.CONFIG["stop_loss"] = orig_sl
            IndicatorEngine.atr = orig_atr

    def test_sell_returns_trade_result(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        result = engine.run_once(df=_downtrend_df())
        trade = result["trade"]
        assert trade is not None
        assert "entry_price" in trade
        assert "exit_price" in trade
        assert "net_pnl" in trade
        assert "exit_reason" in trade

    def test_sell_updates_balance(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        before = engine.current_balance()
        engine.run_once(df=_downtrend_df())
        after = engine.current_balance()
        assert after != before

    def test_sell_adds_to_trade_history(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        assert len(engine.trade_history()) == 0
        engine.run_once(df=_downtrend_df())
        assert len(engine.trade_history()) == 1


# ---------------------------------------------------------------------------
#  No signal (HOLD)
# ---------------------------------------------------------------------------

class TestNoSignal:

    def test_hold_does_not_open_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_sideways_df())
        assert result["position"] is None
        assert result["trade"] is None

    def test_hold_returns_signal(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_sideways_df())
        assert result["signal"] is not None
        assert result["signal"]["signal"] == "HOLD"

    def test_hold_preserves_balance(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_sideways_df())
        assert engine.current_balance() == 10_000.0

    def test_hold_no_trade_history(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_sideways_df())
        assert engine.trade_history() == []

    def test_hold_statistics_empty(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_sideways_df())
        stats = engine.statistics()
        assert stats["total_trades"] == 0


# ---------------------------------------------------------------------------
#  Take Profit
# ---------------------------------------------------------------------------

class TestTakeProfit:

    def test_tp_closes_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        assert engine.current_position() is not None

        pos = engine.current_position()
        assert pos is not None
        tp_df = _tp_hit_df(pos["entry_price"])
        result = engine.run_once(df=tp_df)
        assert result["trade"] is not None
        assert result["trade"]["exit_reason"] == "Take Profit"

    def test_tp_is_profitable(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        assert result["trade"]["net_pnl"] > 0

    def test_tp_clears_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        assert engine.current_position() is None

    def test_tp_adds_to_trade_history(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        assert len(engine.trade_history()) == 1

    def test_tp_holding_time_positive(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        assert result["trade"] is not None
        assert result["trade"]["holding_time"].total_seconds() > 0

    def test_tp_entry_time_is_timezone_aware(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        assert pos["entry_time"].tzinfo is not None
        assert pos["entry_time"].tzinfo == timezone.utc

    def test_tp_closed_trade_entry_time_is_timezone_aware(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        trade = result["trade"]
        assert trade is not None
        assert trade["entry_time"].tzinfo is not None
        assert trade["entry_time"].tzinfo == timezone.utc


# ---------------------------------------------------------------------------
#  Stop Loss
# ---------------------------------------------------------------------------

class TestStopLoss:

    def test_sl_closes_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        assert engine.current_position() is not None

        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        assert result["trade"] is not None
        assert result["trade"]["exit_reason"] == "Stop Loss"

    def test_sl_is_loss(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        assert result["trade"]["net_pnl"] < 0

    def test_sl_clears_position(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        assert engine.current_position() is None


# ---------------------------------------------------------------------------
#  Position monitoring (no exit, no BUY)
# ---------------------------------------------------------------------------

class TestPositionMonitoring:

    def test_position_remains_open_when_no_exit(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        assert engine.current_position() is not None

        engine.run_once(df=_uptrend_buy_df())
        assert engine.current_position() is not None

    def test_no_buy_when_position_open(self) -> None:
        """Engine must not open a second position when one exists."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())

        pos_before = engine.current_position()
        engine.run_once(df=_uptrend_buy_df())  # still BUY signal but position exists
        pos_after = engine.current_position()

        assert pos_before is not None
        assert pos_after is not None
        assert pos_before["entry_price"] == pos_after["entry_price"]

    def test_monitoring_tp_not_missed(self) -> None:
        """If price hits TP during monitor cycle, close."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        result = engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        assert result["trade"] is not None
        assert result["trade"]["exit_reason"] == "Take Profit"


# ---------------------------------------------------------------------------
#  Duplicate BUY prevention
# ---------------------------------------------------------------------------

class TestDuplicateBuyPrevention:

    def test_no_double_buy(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        qty1 = engine.current_position()["quantity"]

        # Second BUY should be ignored
        engine.run_once(df=_uptrend_buy_df())
        qty2 = engine.current_position()["quantity"]
        assert qty1 == qty2

    def test_buy_after_sell_works(self) -> None:
        """After SELL, a new BUY should open a fresh position."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        assert engine.current_position() is None

        engine.run_once(df=_uptrend_buy_df())
        assert engine.current_position() is not None

    def test_repeated_cycles_no_duplicate_buy_position(self) -> None:
        """Many cycles with a position open must never open a second."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        entry_price = pos["entry_price"]
        qty = pos["quantity"]

        for _ in range(10):
            engine.run_once(df=_uptrend_buy_df())
            p = engine.current_position()
            assert p is not None
            assert p["entry_price"] == entry_price
            assert p["quantity"] == qty
        assert len(engine.trade_history()) == 0

    def test_repeated_cycles_no_duplicate_buy_notification(self) -> None:
        """Must not send duplicate BUY notifications for the same position."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._notifier = MagicMock()
        engine._notifier.buy_opened = MagicMock()

        engine.run_once(df=_uptrend_buy_df())
        engine._notifier.buy_opened.assert_called_once()

        for _ in range(5):
            engine.run_once(df=_uptrend_buy_df())
        engine._notifier.buy_opened.assert_called_once()

    def test_buy_notification_after_tp_resends(self) -> None:
        """After TP close, a NEW BUY should send a new notification."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._notifier = MagicMock()
        engine._notifier.buy_opened = MagicMock()
        engine._notifier.trade_closed = MagicMock()

        engine.run_once(df=_uptrend_buy_df())
        engine._notifier.buy_opened.assert_called_once()

        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        engine._notifier.trade_closed.assert_called_once()

        engine.run_once(df=_uptrend_buy_df())
        assert engine._notifier.buy_opened.call_count == 2


# ---------------------------------------------------------------------------
#  Statistics
# ---------------------------------------------------------------------------

class TestStatistics:

    def test_statistics_after_one_trade(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        engine.run_once(df=_downtrend_df())
        stats = engine.statistics()
        assert stats["total_trades"] == 1

    def test_statistics_after_won_trade(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["win_count"] == 1
        assert stats["loss_count"] == 0
        assert stats["win_rate"] == 100.0

    def test_statistics_after_lost_trade(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["win_count"] == 0
        assert stats["loss_count"] == 1
        assert stats["loss_rate"] == 100.0

    def test_statistics_after_multiple_trades(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        # Trade 1: win
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        # Trade 2: loss
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["total_trades"] == 2
        assert stats["win_count"] == 1
        assert stats["loss_count"] == 1
        assert stats["win_rate"] == 50.0
        assert stats["loss_rate"] == 50.0

    def test_total_profit_tracks_correctly(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["total_profit"] > 0

    def test_profit_factor_infinite_on_all_wins(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["profit_factor"] == float("inf")

    def test_longest_streak_tracks(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        for _ in range(3):
            engine.run_once(df=_uptrend_buy_df())
            pos = engine.current_position()
            assert pos is not None
            engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        stats = engine.statistics()
        assert stats["longest_win_streak"] == 3
        assert stats["longest_loss_streak"] == 0


# ---------------------------------------------------------------------------
#  Balance updates
# ---------------------------------------------------------------------------

class TestBalanceUpdates:

    def test_balance_decreases_on_loss(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        before = engine.current_balance()
        engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        after = engine.current_balance()
        assert after < before

    def test_balance_increases_on_profit(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        before = engine.current_balance()
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        after = engine.current_balance()
        assert after > before

    def test_balance_unchanged_on_hold(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        before = engine.current_balance()
        engine.run_once(df=_sideways_df())
        assert engine.current_balance() == before


# ---------------------------------------------------------------------------
#  Status query
# ---------------------------------------------------------------------------

class TestStatus:

    def test_status_returns_dict(self) -> None:
        engine = PaperTradingEngine()
        status = engine.status()
        assert isinstance(status, dict)

    def test_status_has_required_keys(self) -> None:
        engine = PaperTradingEngine()
        status = engine.status()
        required = {"state", "balance", "position", "last_signal",
                     "last_price", "market_state"}
        assert required.issubset(status.keys())

    def test_status_balance_after_trade(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        status = engine.status()
        assert status["balance"] != 10_000.0


# ---------------------------------------------------------------------------
#  run_once return value
# ---------------------------------------------------------------------------

class TestRunOnceReturn:

    def test_returns_dict(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_sideways_df())
        assert isinstance(result, dict)

    def test_no_signal_returns_trade_none(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_sideways_df())
        assert result["trade"] is None

    def test_buy_returns_trade_none(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_uptrend_buy_df())
        assert result["trade"] is None  # no close yet

    def test_price_is_float(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=_uptrend_buy_df())
        assert isinstance(result["price"], float)


# ---------------------------------------------------------------------------
#  Full lifecycle (BUY → MONITOR → SELL)
# ---------------------------------------------------------------------------

class TestFullLifecycle:

    def test_complete_cycle_buy_monitor_sell(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)

        # Phase 1: BUY
        r1 = engine.run_once(df=_uptrend_buy_df())
        assert r1["position"] is not None
        pos = r1["position"]
        assert pos is not None
        entry = pos["entry_price"]

        # Phase 2: MONITOR (no exit)
        r2 = engine.run_once(df=_uptrend_buy_df())
        assert r2["trade"] is None
        assert r2["position"] is not None

        # Phase 3: SELL (TP hit)
        r3 = engine.run_once(df=_tp_hit_df(entry))
        assert r3["trade"] is not None
        assert r3["trade"]["exit_reason"] == "Take Profit"
        assert r3["position"] is None

        # Verify trade recorded
        assert len(engine.trade_history()) == 1
        stats = engine.statistics()
        assert stats["total_trades"] == 1

    def test_complete_cycle_buy_stop_loss(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)

        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None

        result = engine.run_once(df=_sl_hit_df(pos["entry_price"]))
        assert result["trade"] is not None
        assert result["trade"]["exit_reason"] == "Stop Loss"
        assert result["trade"]["net_pnl"] < 0
        assert engine.current_position() is None

    def test_complete_cycle_buy_strategy_exit(self) -> None:
        import bot.config as cfg
        from bot.indicators import IndicatorEngine
        orig_sl = cfg.CONFIG.get("stop_loss")
        cfg.CONFIG["stop_loss"] = 10.0
        # Force the ATR path unavailable so PaperTrader falls back to
        # the wide fixed % above — see test_sell_closes_position for
        # why (this test targets Strategy-Exit, not stop sizing).
        orig_atr = IndicatorEngine.atr
        IndicatorEngine.atr = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("no atr in test"))
        )
        try:
            engine = PaperTradingEngine(initial_balance=10_000.0)
            engine.run_once(df=_uptrend_buy_df())
            pos = engine.current_position()
            assert pos is not None, "BUY should open a position"

            entry = pos["entry_price"]
            sl = pos["stop_loss_price"]
            n = 250
            close = [60_000.0 - i * 36.0 for i in range(n)]
            exit_df = pd.DataFrame({
                "high": [c + 100.0 for c in close],
                "low":  [c - 100.0 for c in close],
                "close": close,
            })
            result = engine.run_once(df=exit_df)
            assert result["trade"] is not None, f"No trade: exit_price={close[-1]:.1f} sl={sl:.1f}"
            assert result["trade"]["exit_reason"] == "Strategy Exit"
            assert result["trade"]["net_pnl"] != 0
            assert engine.current_position() is None
        finally:
            cfg.CONFIG["stop_loss"] = orig_sl
            IndicatorEngine.atr = orig_atr

    def test_multiple_full_cycles(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)

        for i in range(3):
            engine.run_once(df=_uptrend_buy_df())
            pos = engine.current_position()
            assert pos is not None, f"Cycle {i}: no position after BUY"
            engine.run_once(df=_tp_hit_df(pos["entry_price"]))
            assert engine.current_position() is None, f"Cycle {i}: position not closed"

        assert len(engine.trade_history()) == 3
        stats = engine.statistics()
        assert stats["total_trades"] == 3
        assert stats["win_count"] == 3


# ---------------------------------------------------------------------------
#  MarketData integration (live fetch)
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestMarketDataIntegration:

    @pytest.mark.network
    def test_engine_accepts_no_df_fetches_live(self) -> None:
        """Verify run_once() works without pre-fetched df (live fetch)."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once()
        assert isinstance(result, dict)
        assert "price" in result
        assert isinstance(result["price"], float)
        assert result["price"] > 0

    @pytest.mark.network
    def test_live_fetch_maintains_state(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once()
        assert result["state"] == IDLE
        assert result["signal"] is not None


# ---------------------------------------------------------------------------
#  Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_large_dataset(self) -> None:
        """1000 candles should not crash."""
        n = 1000
        import random
        random.seed(42)
        close = [50_000.0]
        highs, lows = [], []
        for i in range(1, n):
            c = close[-1] + random.uniform(-200, 200)
            close.append(c)
            highs.append(c + 100.0)
            lows.append(c - 100.0)
        highs.insert(0, close[0] + 100.0)
        lows.insert(0, close[0] - 100.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})

        engine = PaperTradingEngine(initial_balance=10_000.0)
        result = engine.run_once(df=df)
        assert isinstance(result, dict)
        assert "state" in result

    def test_small_dataset_raises(self) -> None:
        """Too few candles should raise."""
        engine = PaperTradingEngine(initial_balance=10_000.0)
        df = pd.DataFrame({
            "high": [100.0, 101.0],
            "low":  [99.0, 100.0],
            "close":[100.0, 101.0],
        })
        with pytest.raises(Exception):
            engine.run_once(df=df)

    def test_engine_trade_history_is_copy(self) -> None:
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine.run_once(df=_uptrend_buy_df())
        pos = engine.current_position()
        assert pos is not None
        engine.run_once(df=_tp_hit_df(pos["entry_price"]))
        h1 = engine.trade_history()
        h2 = engine.trade_history()
        assert h1 is not h2
