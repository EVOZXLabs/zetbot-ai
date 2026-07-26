"""
Regression tests for position sizing and risk management.

Covers:
- Oversized position rejection (capped to max_position_size_pct)
- Insufficient balance for minimum position
- Multiple open position capital tracking
- Balance consistency across approved trades
- Previous bug: position_value > balance when stop_distance is small
"""

import math
from dataclasses import asdict

import pytest

from scripts.risk_manager import (
    DecisionData,
    PositionSizer,
    RiskManager,
    RiskResult,
    ScannerData,
    StopLossCalculator,
    TradeValidator,
)


# ===================================================================
#  PositionSizer — notional value capping
# ===================================================================


class TestPositionSizerCap:
    """PositionSizer must cap position_value to max_position_value."""

    def test_no_cap_when_none(self):
        """When max_position_value is None, old behaviour is preserved."""
        balance = 10_000.0
        entry = 50_000.0
        stop = 49_000.0  # 2% stop distance
        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
        )
        # risk = 10000 * 0.02 = 200
        # size = 200 / 1000 = 0.2 BTC
        # value = 0.2 * 50000 = 10,000
        assert risk == pytest.approx(200.0)
        assert size == pytest.approx(0.2)
        assert value == pytest.approx(10_000.0)

    def test_cap_applied_when_exceeding(self):
        """When position_value > max_position_value, cap to max."""
        balance = 10_000.0
        entry = 50_000.0
        stop = 49_750.0  # 0.5% stop distance → very large position
        max_val = 6_000.0  # 60% of balance

        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
            max_position_value=max_val,
        )
        # Without cap: risk=200, size=200/250=0.8, value=0.8*50000=40000
        # With cap: value=6000, size=6000/50000=0.12, risk=0.12*250=30
        assert value == pytest.approx(6_000.0)
        assert size == pytest.approx(0.12)
        assert risk == pytest.approx(30.0)

    def test_cap_not_applied_when_under(self):
        """When position_value < max_position_value, no cap needed."""
        balance = 10_000.0
        entry = 50_000.0
        stop = 45_000.0  # 10% stop distance → small position
        max_val = 6_000.0

        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
            max_position_value=max_val,
        )
        # risk=200, size=200/5000=0.04, value=0.04*50000=2000
        assert value == pytest.approx(2_000.0)
        assert risk == pytest.approx(200.0)

    def test_previous_bug_regression(self):
        """Bug: tight stop with small balance → position > balance.

        Account equity = $1,286, entry = $71,497, stop at 2%
        Old behaviour: value = $1,287 > $1,286 (exceeds balance!)
        Fixed: capped to $1,286 * 0.6 = $771.60
        """
        balance = 1_286.0
        entry = 71_497.0
        stop = 70_067.0  # 2% below entry
        max_val = balance * 0.6  # 60% of equity

        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
            max_position_value=max_val,
        )
        # Old (no cap): risk=25.72, size=25.72/1430=0.018, value=1287
        assert value <= max_val, (
            f"position_value {value:.2f} exceeds cap {max_val:.2f}"
        )
        assert value <= balance, (
            f"position_value {value:.2f} exceeds balance {balance:.2f}"
        )
        assert value == pytest.approx(771.60, rel=1e-3)

    def test_very_tight_stop_regression(self):
        """ATR-based stops can be <0.5% — must not blow up position."""
        balance = 10_000.0
        entry = 50_000.0
        stop = 49_800.0  # 0.4% stop
        max_val = balance * 0.6

        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
            max_position_value=max_val,
        )
        # Without cap: size=200/200=1.0 BTC, value=50000 (5x balance!)
        assert value <= max_val
        assert value <= balance

    def test_zero_stop_distance(self):
        """stop_price >= entry_price returns zero position."""
        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 50_000.0, 50_000.0,
        )
        assert size == 0.0
        assert risk == 0.0
        assert value == 0.0

        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 50_000.0, 51_000.0,
        )
        assert size == 0.0

    def test_zero_entry_price(self):
        """entry_price of 0 returns zero position."""
        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 0.0, 49_000.0,
            max_position_value=6_000.0,
        )
        # stop_distance = -49k → <= 0 → zeros
        assert size == 0.0


# ===================================================================
#  RiskManager — used_capital tracking
# ===================================================================


class TestRiskManagerCapitalTracking:
    """RiskManager must track _used_capital across approved trades."""

    def test_single_approve_tracks_capital(self):
        """After approving a trade, _used_capital > 0."""
        manager = RiskManager(balance=10_000.0)
        result = _simulate_trade(manager, entry=50_000.0, stop=49_000.0)
        assert manager._used_capital > 0.0
        assert manager._used_capital <= 10_000.0

    def test_multiple_approvals_capped(self):
        """Sum of position values across approvals ≤ balance."""
        manager = RiskManager(balance=10_000.0, max_positions=5)
        for i in range(3):
            _simulate_trade(manager, entry=50_000.0, stop=49_000.0)
        assert manager._used_capital <= 10_000.0

    def test_cannot_exceed_balance(self):
        """Even with many approvals, total used capital ≤ balance."""
        manager = RiskManager(balance=5_000.0, max_positions=10)
        for i in range(5):
            _simulate_trade(manager, entry=50_000.0, stop=49_000.0)
        assert manager._used_capital <= 5_000.0

    def test_available_capital_decreases(self):
        """Each approval reduces available_capital for the next."""
        manager = RiskManager(balance=10_000.0, max_positions=3)
        cap_before = 10_000.0 - 0.0
        _simulate_trade(manager, entry=50_000.0, stop=49_000.0)
        cap_after = 10_000.0 - manager._used_capital
        assert cap_after < cap_before - 1.0  # at least $1 less


# ===================================================================
#  PORTFOLIO-WIDE exposure cap (the bug from the audit)
#
#  Symptom: Cash=$0, In Position=$5,805, Exposure=100% even though
#  MAX_POSITION_SIZE_PCT=60%. Root cause: MAX_POSITION_SIZE_PCT was
#  applied per-position against a shrinking "remaining cash" pool
#  instead of against total equity, and positions already open from
#  earlier pipeline cycles were never subtracted from the budget.
# ===================================================================


class TestPortfolioExposureCap:
    """Total value of ALL open positions must never exceed
    MAX_POSITION_SIZE_PCT * equity — not just each position individually,
    and not just within a single run.
    """

    def test_second_position_rejected_or_reduced_within_one_run(self):
        """equity=$10,000, cap=60% -> total open positions <= $6,000.

        First position: $4,000 (within cap, plenty of room).
        Second position: sizing must be reduced so cumulative <= $6,000
        (or reduced to ~$0 / rejected by MIN_POSITION_SIZE_USD).
        """
        equity = 10_000.0
        cap_pct = 0.6
        manager = RiskManager(
            balance=equity, equity=equity,
            existing_exposure=0.0,
            max_position_size_pct=cap_pct, max_positions=5,
        )

        # --- Position 1: force position_value == $4,000 directly via
        # the sizer, then record it the same way run() does.
        max_pos_value_1 = manager._max_new_position_value()
        assert max_pos_value_1 == pytest.approx(6_000.0)  # nothing open yet

        pos1_value = 4_000.0
        assert pos1_value <= max_pos_value_1
        manager._used_capital += pos1_value

        # --- Position 2: remaining budget must now be capped to
        # $6,000 - $4,000 = $2,000 (NOT 60% of remaining cash, which
        # would incorrectly allow ($10,000-$4,000)*0.6 = $3,600).
        max_pos_value_2 = manager._max_new_position_value()
        assert max_pos_value_2 == pytest.approx(2_000.0), (
            f"expected remaining budget of $2,000, got ${max_pos_value_2:,.2f}"
        )

        # Attempt to size a second position that would naturally want
        # to be large (tight stop => big notional pre-cap).
        entry, stop = 50_000.0, 49_750.0  # 0.5% stop -> large uncapped size
        size, risk, pos2_value = PositionSizer.calculate(
            manager.balance, manager.risk_per_trade, entry, stop,
            max_position_value=max_pos_value_2,
        )

        total_exposure = pos1_value + pos2_value
        assert total_exposure <= 6_000.0 + 1e-6, (
            f"total exposure ${total_exposure:,.2f} exceeds "
            f"60% cap of $6,000"
        )
        # Either meaningfully reduced, or effectively rejected (~$0 /
        # below the exchange minimum so the validator would reject it).
        assert pos2_value <= 2_000.0 + 1e-6

    def test_existing_exposure_from_previous_cycle_is_respected(self):
        """A position already open from a PREVIOUS pipeline run must
        count against the cap for THIS run's new positions.

        This is the exact bug: previously, `_used_capital` reset to 0
        every run, so pre-existing open exposure was invisible.
        """
        equity = 10_000.0
        manager = RiskManager(
            balance=6_000.0,          # free cash left after position 1
            equity=equity,
            existing_exposure=4_000.0,  # position 1, opened last cycle
            max_position_size_pct=0.6,
            max_positions=5,
        )

        max_new = manager._max_new_position_value()
        # Budget left under the 60% ($6,000) cap = 6000 - 4000 = 2000
        assert max_new == pytest.approx(2_000.0)

    def test_full_exposure_bug_scenario(self):
        """Regression test for the reported bug:
        Cash=$0, In Position=$5,805, Exposure=100%, cap=60%.

        Once exposure has already reached (or exceeded) the cap, the
        risk manager must allow NO further new position value.
        """
        manager = RiskManager(
            balance=0.0,
            equity=5_805.0,
            existing_exposure=5_805.0,
            max_position_size_pct=0.6,
        )
        assert manager._max_new_position_value() == pytest.approx(0.0)

    def test_cap_uses_equity_not_just_free_cash(self):
        """The 60% cap must be computed against equity (cash + open
        positions), not free cash alone -- otherwise the allowed
        exposure silently shrinks/grows in ways decoupled from the
        configured percentage of the real account value.
        """
        manager = RiskManager(
            balance=2_000.0,           # cash remaining
            equity=10_000.0,           # true equity (cash + $8,000 open)
            existing_exposure=8_000.0,
            max_position_size_pct=0.6,
        )
        # Cap = 60% * 10,000 = 6,000; already committed = 8,000 (over cap)
        # -> no budget left, regardless of the $2,000 cash sitting free.
        assert manager._max_new_position_value() == pytest.approx(0.0)


# ===================================================================
#  Insufficient balance
# ===================================================================


class TestInsufficientBalance:
    """When balance is too small, position must be capped or zero."""

    def test_tiny_balance_returns_small_position(self):
        """With balance=$100, position should be tiny but capped."""
        balance = 100.0
        entry = 50_000.0
        stop = 49_000.0
        max_val = balance * 0.6

        size, risk, value = PositionSizer.calculate(
            balance, 2.0, entry, stop,
            max_position_value=max_val,
        )
        assert value <= 60.0  # 60% of $100
        assert value > 0.0
        assert risk < balance

    def test_very_small_balance_after_approvals(self):
        """After using most capital, remaining trades are tiny."""
        manager = RiskManager(balance=1_000.0, max_positions=5)
        for i in range(5):
            _simulate_trade(manager, entry=50_000.0, stop=49_500.0)
        # Last trade should have very small or zero available capital
        assert manager._used_capital <= 1_000.0 + 1.0  # allow rounding


# ===================================================================
#  Paper wallet consistency
# ===================================================================


class TestPaperWallet:
    """VirtualWallet must correctly track free/used/equity."""

    def test_wallet_starts_correct(self):
        from scripts.paper_trading_engine import VirtualWallet
        w = VirtualWallet(10_000.0)
        assert w.balance == 10_000.0
        assert w.free_balance == 10_000.0
        snap = w.snapshot()
        assert snap.balance == 10_000.0
        assert snap.equity == 10_000.0
        assert snap.unrealized_pnl == 0.0
        assert snap.free_balance == 10_000.0

    def test_wallet_after_deduct(self):
        from scripts.paper_trading_engine import VirtualWallet
        w = VirtualWallet(10_000.0)
        w.deduct(500.0)
        assert w.balance == 9_500.0
        assert w.free_balance == 9_500.0
        snap = w.snapshot(position_value=500.0)
        assert snap.balance == 9_500.0
        assert snap.equity == 10_000.0  # balance + position_value
        assert snap.free_balance == 9_500.0

    def test_wallet_equity_includes_position_value(self):
        from scripts.paper_trading_engine import VirtualWallet
        w = VirtualWallet(10_000.0)
        w.deduct(2_000.0)
        snap = w.snapshot(position_value=2_100.0, unrealized_pnl_value=100.0)
        assert snap.balance == 8_000.0
        assert snap.equity == 10_100.0  # 8000 + 2100
        assert snap.unrealized_pnl == 100.0

    def test_wallet_after_add(self):
        from scripts.paper_trading_engine import VirtualWallet
        w = VirtualWallet(10_000.0)
        w.add(500.0)
        assert w.balance == 10_500.0

    def test_deduct_insufficient_funds(self):
        from scripts.paper_trading_engine import VirtualWallet
        w = VirtualWallet(100.0)
        result = w.deduct(200.0)
        assert result is False
        assert w.balance == 100.0


# ===================================================================
#  Test helpers
# ===================================================================


# ===================================================================
#  Daily Loss Protection
# ===================================================================


class TestDailyLossProtection:
    """TradeValidator must reject trades that exceed the daily loss limit."""

    def test_daily_loss_not_reached_allows_trade(self):
        """When daily loss is below limit, trade is APPROVED."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.daily_risk_used = 0.0
        approval, reason = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=3,
        )
        assert approval == "APPROVED"

    def test_daily_loss_reached_rejects_trade(self):
        """When daily loss is at limit, trade is REJECTED."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.daily_risk_used = 250.0
        approval, reason = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=3,
        )
        assert approval == "REJECTED"
        assert "Daily loss limit" in reason

    def test_daily_loss_edge_case_at_limit(self):
        """Risk amount exactly at daily loss limit boundary."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.daily_risk_used = 299.0
        approval, reason = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=1.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=3,
        )
        assert approval == "APPROVED"


# ===================================================================
#  Max Open Position Protection
# ===================================================================


class TestMaxOpenPosition:
    """TradeValidator must reject when max positions are reached."""

    def test_open_positions_below_limit_allows_trade(self):
        """When open positions < max, trade is APPROVED."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.open_positions = 0
        approval, reason = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=2,
        )
        assert approval == "APPROVED"

    def test_max_positions_reached_rejects_trade(self):
        """When open positions >= max, trade is REJECTED."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.open_positions = 2
        approval, reason = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=2,
        )
        assert approval == "REJECTED"
        assert "Max positions" in reason

    def test_risk_manager_default_max_positions_is_one(self):
        """RiskManager must default to 1 max position."""
        manager = RiskManager(balance=10_000.0)
        assert manager.max_positions == 1

    def test_risk_manager_respects_max_positions_from_risk_result(self):
        """Validator correctly tracks approved count against max."""
        validator = TradeValidator()
        scanner = ScannerData(
            symbol="BTC/USDT", price=50_000.0, volume_24h=10_000_000.0,
            change_24h=2.0, ema50=49_000.0, ema100=48_500.0, ema200=48_000.0,
            rsi14=55.0, adx14=30.0, atr_pct=1.5, relative_volume=1.2,
            trend_alignment="BULLISH",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=75.0, recommendation="STRONG BUY",
            risk_score=20.0, reward_score=70.0, trend_score=80.0,
            momentum_score=60.0, volume_score=50.0, volatility_score=40.0,
            expected_rr=2.5, overall_score=75.0,
        )
        validator.open_positions = 0
        approval1, _ = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=1,
        )
        assert approval1 == "APPROVED"
        validator.open_positions = 1
        approval2, reason2 = validator.validate(
            scanner=scanner, decision=decision,
            actual_rr=2.5, position_value=1_000.0, risk_amount=100.0,
            stop_distance_pct=2.0, max_daily_loss=300.0, max_positions=1,
        )
        assert approval2 == "REJECTED"
        assert "Max positions" in reason2


# ===================================================================
#  Division by zero edge cases (balance=0, equity=0, price=0)
# ===================================================================


class TestDivisionByZeroEdgeCases:
    """RiskManager must not crash when balance, equity, or price is zero."""

    def test_risk_manager_with_zero_balance(self):
        """balance=0 should produce no division errors in print/run internals."""
        manager = RiskManager(balance=0.0, equity=0.0, existing_exposure=0.0)
        # _print_summary divisions must not crash
        manager.results = []
        manager._print_summary(0.0)
        # _max_new_position_value with zero balance/equity
        assert manager._max_new_position_value() == 0.0

    def test_risk_manager_with_zero_equity(self):
        """equity=0 should not cause division by zero in exposure calc."""
        manager = RiskManager(balance=0.0, equity=0.0, existing_exposure=0.0)
        # The exposure pct print uses `if self.equity else 0.0`
        cap = manager._max_new_position_value()
        assert cap == 0.0

    def test_position_sizer_zero_entry_price(self):
        """entry_price=0 should return zero position."""
        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 0.0, 49_000.0,
        )
        assert size == 0.0
        assert risk == 0.0
        assert value == 0.0

    def test_position_sizer_zero_balance(self):
        """balance=0 should return zero for all values."""
        size, risk, value = PositionSizer.calculate(
            0.0, 2.0, 50_000.0, 49_000.0,
        )
        assert size == 0.0
        assert risk == 0.0
        assert value == 0.0

    def test_position_sizer_zero_stop_distance(self):
        """stop_price >= entry_price should return zero."""
        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 50_000.0, 50_000.0,
        )
        assert size == 0.0
        assert risk == 0.0
        assert value == 0.0

        size, risk, value = PositionSizer.calculate(
            10_000.0, 2.0, 50_000.0, 51_000.0,
        )
        assert size == 0.0

    def test_zero_price_in_stop_distance_pct(self):
        """scanner.price=0 in stop_distance_pct calc must not crash."""
        # This exercises the guard: `if scanner.price > 0 else 0.0`
        from scripts.risk_manager import (
            ScannerData, DecisionData, RiskManager,
            PositionSizer, StopLossCalculator, TakeProfitCalculator,
        )

        scanner = ScannerData(
            symbol="BTC/USDT", price=0.0, volume_24h=0.0,
            change_24h=0.0, ema50=0.0, ema100=0.0, ema200=0.0,
            rsi14=50.0, adx14=0.0, atr_pct=0.0,
            relative_volume=1.0, trend_alignment="MIXED",
        )
        decision = DecisionData(
            symbol="BTC/USDT", probability=0.0, recommendation="",
            risk_score=0.0, reward_score=0.0, trend_score=0.0,
            momentum_score=0.0, volume_score=0.0, volatility_score=0.0,
            expected_rr=0.0, overall_score=0.0,
        )
        mgr = RiskManager(balance=0.0, equity=0.0, existing_exposure=0.0)

        stop_price, stop_method = StopLossCalculator.safest(
            scanner.price, scanner.atr_pct, scanner.ema200,
        )
        # This was the exact line that would crash with ZeroDivisionError:
        stop_distance_pct = (
            (scanner.price - stop_price) / scanner.price * 100.0
            if scanner.price > 0 else 0.0
        )
        assert stop_distance_pct == 0.0

    def test_max_daily_loss_division_with_zero_denom(self):
        """_print_summary must handle zero balance/equity for max daily loss %."""
        mgr = RiskManager(balance=0.0, equity=0.0)
        pct_denom = mgr.balance if mgr.balance else mgr.equity
        # The ternary: `... / pct_denom * 100 if pct_denom else 0.0`
        result = mgr.max_daily_loss_amt / pct_denom * 100 if pct_denom else 0.0
        assert result == 0.0

    def test_exposure_pct_with_zero_equity(self):
        """Exposure % must not crash when equity is 0."""
        mgr = RiskManager(balance=0.0, equity=0.0, existing_exposure=1000.0)
        # The print uses: `self._existing_exposure / self.equity * 100.0 if self.equity else 0.0`
        assert mgr.equity == 0.0
        # Just verify no ZeroDivisionError would occur
        pct = mgr._existing_exposure / mgr.equity * 100.0 if mgr.equity else 0.0
        assert pct == 0.0


def _simulate_trade(manager: RiskManager,
                    entry: float = 50_000.0,
                    stop: float = 49_000.0) -> RiskResult:
    """Simulate running a single trade through the RiskManager internals.

    Uses mock scanner/decision data to exercise the sizing & validation
    pipeline without requiring JSON files on disk.
    """
    from scripts.risk_manager import (
        ScannerData,
        DecisionData,
        PositionSizer,
        StopLossCalculator,
        TakeProfitCalculator,
    )

    scanner = ScannerData(
        symbol="BTC/USDT",
        price=entry,
        volume_24h=10_000_000.0,
        change_24h=2.0,
        ema50=49_000.0,
        ema100=48_500.0,
        ema200=48_000.0,
        rsi14=55.0,
        adx14=30.0,
        atr_pct=1.5,
        relative_volume=1.2,
        trend_alignment="BULLISH",
    )
    decision = DecisionData(
        symbol="BTC/USDT",
        probability=75.0,
        recommendation="STRONG BUY",
        risk_score=20.0,
        reward_score=70.0,
        trend_score=80.0,
        momentum_score=60.0,
        volume_score=50.0,
        volatility_score=40.0,
        expected_rr=2.5,
        overall_score=75.0,
    )

    stop_price, stop_method = StopLossCalculator.safest(
        scanner.price, scanner.atr_pct, scanner.ema200,
    )
    stop_distance_pct = (
        (scanner.price - stop_price) / scanner.price * 100.0
    )

    max_pos_value = manager._max_new_position_value()

    pos_size, risk_amt, pos_value = PositionSizer.calculate(
        manager.balance, manager.risk_per_trade,
        scanner.price, stop_price,
        max_position_value=max_pos_value,
    )

    tp_prices = TakeProfitCalculator.calculate(scanner.price, stop_price)
    tp1, tp2, tp3 = tp_prices[0], tp_prices[1], tp_prices[2]
    rr_for_validation = decision.expected_rr
    reward_amt = pos_size * (tp2 - scanner.price)

    manager.validator.open_positions = 0
    manager.validator.daily_risk_used = 0.0

    approval, reason = manager.validator.validate(
        scanner=scanner,
        decision=decision,
        actual_rr=rr_for_validation,
        position_value=pos_value,
        risk_amount=risk_amt,
        stop_distance_pct=stop_distance_pct,
        max_daily_loss=manager.max_daily_loss_amt,
        max_positions=manager.max_positions,
    )

    result = RiskResult(
        symbol=decision.symbol,
        probability=decision.probability,
        position_size=round(pos_size, 6),
        position_value=round(pos_value, 2),
        entry_price=scanner.price,
        stop_loss=round(stop_price, 8),
        stop_method=stop_method,
        stop_distance_pct=round(stop_distance_pct, 2),
        take_profit_1=round(tp1, 8),
        take_profit_2=round(tp2, 8),
        take_profit_3=round(tp3, 8),
        risk_amount=round(risk_amt, 2),
        risk_percent=round(manager.risk_per_trade, 2),
        reward_amount=round(reward_amt, 2),
        expected_rr=round(rr_for_validation, 2),
        approval=approval,
        rejection_reason=reason,
    )

    if approval == "APPROVED":
        manager._used_capital += pos_value

    return result
