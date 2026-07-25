"""
Tests for the Money Management Engine (scripts/money_management.py).

Covers:
- Risk Percentage calculation (default production mode)
- Dynamic position sizing across account sizes ($10 - $10,000)
- Stop Loss / Take Profit defaults (1.5% / 3%)
- Fixed Amount / Percentage Balance / Compounding modes
- Position value never exceeds balance
- Daily loss limit / max open positions defaults
"""

import pytest

from scripts.money_management import (
    DAILY_LOSS_LIMIT,
    DEFAULT_MODE,
    MAX_OPEN_POSITIONS,
    MoneyManagementConfig,
    MoneyManagementMode,
    RISK_PER_TRADE,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    calculate_position_size,
    stop_loss_price,
    take_profit_price,
)


# ===================================================================
#  Production defaults
# ===================================================================


class TestProductionDefaults:
    """SPECIFICATION.md §25/§47 default production values."""

    def test_default_mode_is_risk_percentage(self):
        assert DEFAULT_MODE == MoneyManagementMode.RISK_PERCENTAGE
        assert MoneyManagementConfig().mode == MoneyManagementMode.RISK_PERCENTAGE

    def test_default_risk_per_trade_is_one_percent(self):
        assert RISK_PER_TRADE == pytest.approx(0.01)

    def test_default_stop_loss_is_1_5_percent(self):
        assert STOP_LOSS_PCT == pytest.approx(0.015)

    def test_default_take_profit_is_3_percent(self):
        assert TAKE_PROFIT_PCT == pytest.approx(0.03)

    def test_default_max_open_positions_is_one(self):
        assert MAX_OPEN_POSITIONS == 1

    def test_default_daily_loss_limit_is_3_percent(self):
        assert DAILY_LOSS_LIMIT == pytest.approx(0.03)


# ===================================================================
#  Risk Percentage — the default production mode
# ===================================================================


class TestRiskPercentageCalculation:
    """risk_amount = balance * risk_per_trade
    position_value = risk_amount / stop_loss_pct
    """

    def test_risk_amount_formula(self):
        result = calculate_position_size(10_000.0)
        assert result.risk_amount == pytest.approx(10_000.0 * 0.01)

    def test_position_value_formula(self):
        result = calculate_position_size(10_000.0)
        expected = (10_000.0 * 0.01) / 0.015
        assert result.position_value == pytest.approx(expected)

    def test_custom_risk_and_stop(self):
        cfg = MoneyManagementConfig(risk_per_trade=0.02, stop_loss_pct=0.01)
        result = calculate_position_size(1_000.0, cfg)
        assert result.risk_amount == pytest.approx(20.0)
        # Uncapped formula would be 20 / 0.01 = 2000, but it must never
        # exceed the available balance, so it is capped to 1,000.
        assert result.position_value == pytest.approx(1_000.0)

    def test_position_value_never_exceeds_balance(self):
        cfg = MoneyManagementConfig(risk_per_trade=0.5, stop_loss_pct=0.01)
        result = calculate_position_size(100.0, cfg)
        assert result.position_value <= 100.0


# ===================================================================
#  Dynamic position sizing across account sizes
# ===================================================================


class TestDynamicPositionSizing:
    """Position sizing must scale automatically with account balance —
    $10, $100, $1,000, $10,000 — using the exact same formula/mode.
    """

    @pytest.mark.parametrize("balance", [10.0, 100.0, 1_000.0, 10_000.0])
    def test_scales_proportionally_with_balance(self, balance):
        result = calculate_position_size(balance)
        expected_value = (balance * RISK_PER_TRADE) / STOP_LOSS_PCT
        assert result.position_value == pytest.approx(
            min(expected_value, balance)
        )
        assert result.risk_amount == pytest.approx(balance * RISK_PER_TRADE)

    def test_ratio_is_constant_across_balances(self):
        """position_value / balance should be identical for every size
        (as long as the uncapped value stays below balance)."""
        r10 = calculate_position_size(10.0)
        r10000 = calculate_position_size(10_000.0)
        ratio_small = r10.position_value / 10.0
        ratio_large = r10000.position_value / 10_000.0
        assert ratio_small == pytest.approx(ratio_large)

    def test_small_balance_still_computes(self):
        result = calculate_position_size(10.0)
        assert result.position_value > 0.0

    def test_large_balance_stays_safe(self):
        result = calculate_position_size(10_000.0)
        assert result.position_value <= 10_000.0
        assert result.risk_amount <= 10_000.0 * 0.01 + 1e-9


# ===================================================================
#  Stop Loss / Take Profit helpers
# ===================================================================


class TestStopLossTakeProfitPrices:
    def test_stop_loss_price_default(self):
        assert stop_loss_price(100.0) == pytest.approx(98.5)

    def test_take_profit_price_default(self):
        assert take_profit_price(100.0) == pytest.approx(103.0)

    def test_stop_loss_custom_pct(self):
        assert stop_loss_price(200.0, stop_loss_pct=0.02) == pytest.approx(196.0)

    def test_take_profit_custom_pct(self):
        assert take_profit_price(200.0, take_profit_pct=0.05) == pytest.approx(210.0)


# ===================================================================
#  Other Money Management modes
# ===================================================================


class TestFixedAmountMode:
    def test_uses_fixed_amount(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.FIXED_AMOUNT, fixed_amount=10.0)
        result = calculate_position_size(10_000.0, cfg)
        assert result.position_value == pytest.approx(10.0)

    def test_capped_to_balance_when_smaller(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.FIXED_AMOUNT, fixed_amount=50.0)
        result = calculate_position_size(20.0, cfg)
        assert result.position_value == pytest.approx(20.0)


class TestPercentageBalanceMode:
    def test_default_percentage_is_10_percent(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.PERCENTAGE_BALANCE)
        result = calculate_position_size(1_000.0, cfg)
        assert result.position_value == pytest.approx(100.0)

    def test_scales_with_balance(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.PERCENTAGE_BALANCE)
        r1 = calculate_position_size(100.0, cfg)
        r2 = calculate_position_size(10_000.0, cfg)
        assert r2.position_value == pytest.approx(r1.position_value * 100)


class TestCompoundingMode:
    """Compounding must always use the latest balance (gains/losses
    automatically carried into the next trade's sizing)."""

    def test_same_formula_as_risk_percentage(self):
        risk_cfg = MoneyManagementConfig(mode=MoneyManagementMode.RISK_PERCENTAGE)
        comp_cfg = MoneyManagementConfig(mode=MoneyManagementMode.COMPOUNDING)
        r1 = calculate_position_size(5_000.0, risk_cfg)
        r2 = calculate_position_size(5_000.0, comp_cfg)
        assert r1.position_value == pytest.approx(r2.position_value)

    def test_balance_growth_compounds_next_trade(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.COMPOUNDING)

        # Balance starts at 100, "wins" a trade, grows to 150, then to 500.
        balances = [100.0, 150.0, 500.0]
        sizes = [calculate_position_size(b, cfg).position_value for b in balances]

        # Sizing must increase monotonically with the growing balance —
        # i.e. compounding uses the *latest* balance every time.
        assert sizes[1] > sizes[0]
        assert sizes[2] > sizes[1]

    def test_balance_loss_shrinks_next_trade(self):
        cfg = MoneyManagementConfig(mode=MoneyManagementMode.COMPOUNDING)
        before = calculate_position_size(1_000.0, cfg).position_value
        after = calculate_position_size(700.0, cfg).position_value
        assert after < before


class TestUnknownMode:
    def test_invalid_mode_raises(self):
        cfg = MoneyManagementConfig()
        cfg.mode = "NOT_A_REAL_MODE"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            calculate_position_size(1_000.0, cfg)


# ===================================================================
#  Edge cases
# ===================================================================


class TestEdgeCases:
    def test_zero_balance(self):
        result = calculate_position_size(0.0)
        assert result.position_value == 0.0
        assert result.risk_amount == 0.0

    def test_negative_balance_treated_as_zero(self):
        result = calculate_position_size(-500.0)
        assert result.position_value == 0.0

    def test_max_position_pct_of_balance_cap(self):
        cfg = MoneyManagementConfig(max_position_pct_of_balance=0.5)
        result = calculate_position_size(1_000.0, cfg)
        assert result.position_value <= 500.0
