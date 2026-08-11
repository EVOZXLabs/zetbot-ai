"""
Money Management Engine for ZetBot AI.

Implements SPECIFICATION.md §25 "Money Management Engine" and §47
"Money Management". Determines the notional position VALUE (in quote
currency, e.g. USDT) for the next trade. Money Management never
generates trading signals — it only sizes a trade that the Risk
Manager has already approved (see the trading flow below).

Trading flow (SPECIFICATION.md §21-25)::

    Market Scanner -> Strategy Engine -> Risk Manager
        -> Money Management -> Order Executor

A BUY signal that has not passed the Risk Manager must never reach
Money Management or the Order Executor.

Supported modes
----------------
FIXED_AMOUNT
    Fixed USD amount per trade, capped to available balance.
PERCENTAGE_BALANCE
    Fixed percentage of the current balance.
RISK_PERCENTAGE  (DEFAULT / production mode)
    Position size derived from how much money the account is willing
    to risk and how far away the stop loss is::

        risk_amount   = account_balance * risk_per_trade
        position_size = risk_amount / stop_loss_distance

    Because every mode reads the *current* ``balance`` argument
    instead of any hardcoded absolute figure, sizing automatically
    adapts to any account size ($10, $100, $1,000, $10,000, ...).
COMPOUNDING
    Identical formula to RISK_PERCENTAGE, but explicitly documents
    that the caller must always pass the *latest* balance (including
    realized P&L) so that gains and losses compound into the next
    trade's size automatically.

Usage::

    from scripts.money_management import (
        MoneyManagementConfig, MoneyManagementMode, calculate_position_size,
    )

    cfg = MoneyManagementConfig()  # defaults to RISK_PERCENTAGE
    result = calculate_position_size(balance=1_000.0, config=cfg)
    print(result.position_value)
"""

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
#  Modes
# ---------------------------------------------------------------------------


class MoneyManagementMode(str, Enum):
    """Supported Money Management modes (SPECIFICATION.md §25 / §47)."""

    FIXED_AMOUNT = "FIXED_AMOUNT"
    PERCENTAGE_BALANCE = "PERCENTAGE_BALANCE"
    RISK_PERCENTAGE = "RISK_PERCENTAGE"
    COMPOUNDING = "COMPOUNDING"


# Risk Percentage is the default PRODUCTION mode (SPECIFICATION.md §25).
DEFAULT_MODE: MoneyManagementMode = MoneyManagementMode.RISK_PERCENTAGE

# ---------------------------------------------------------------------------
#  Production defaults (SPECIFICATION.md §25 / §47 / §49-51)
# ---------------------------------------------------------------------------

RISK_PER_TRADE = 0.01        # 1% of balance risked per trade
STOP_LOSS_PCT = 0.015        # 1.5% stop-loss distance
TAKE_PROFIT_PCT = 0.03       # 3% take-profit distance
MAX_OPEN_POSITIONS = 1        # concurrent open positions
DAILY_LOSS_LIMIT = 0.03      # 3% of balance max daily loss

# Fallback defaults for the non-default modes
FIXED_AMOUNT_USDT = 10.0
PERCENTAGE_BALANCE_PCT = 0.10  # legacy "10% of available balance" default

# Absolute USDT floor for a single position (exchange min-notional).
MIN_POSITION_VALUE_USDT = 5.0


@dataclass
class MoneyManagementConfig:
    """Configuration for :func:`calculate_position_size`.

    All percentage-style fields use fractional notation (``0.01`` ==
    1%), matching SPECIFICATION.md's configuration block.
    """

    mode: MoneyManagementMode = DEFAULT_MODE
    risk_per_trade: float = RISK_PER_TRADE
    stop_loss_pct: float = STOP_LOSS_PCT
    take_profit_pct: float = TAKE_PROFIT_PCT
    max_open_positions: int = MAX_OPEN_POSITIONS
    daily_loss_limit: float = DAILY_LOSS_LIMIT
    fixed_amount: float = FIXED_AMOUNT_USDT
    percentage_balance: float = PERCENTAGE_BALANCE_PCT
    # Optional hard cap (fraction of balance) a caller can apply on top
    # of any of the modes above, e.g. a portfolio-wide exposure limit.
    max_position_pct_of_balance: float | None = None


@dataclass
class PositionSizeResult:
    """Result of a Money Management sizing calculation."""

    mode: str
    balance: float
    risk_amount: float
    position_value: float
    stop_loss_pct: float
    take_profit_pct: float


def calculate_position_size(
    balance: float,
    config: MoneyManagementConfig | None = None,
) -> PositionSizeResult:
    """Determine the notional position VALUE (quote currency) to trade.

    Parameters
    ----------
    balance : float
        Current account balance / free cash available to spend. This
        must always be the *live* balance — passing the latest value
        on every call is what makes ``COMPOUNDING`` work correctly.
    config : MoneyManagementConfig | None
        Sizing configuration. Defaults to ``RISK_PERCENTAGE`` mode
        with production defaults (1% risk, 1.5% stop, 3% target).

    Returns
    -------
    PositionSizeResult
        Never exceeds ``balance`` and never goes negative, regardless
        of account size ($10, $100, $1,000, $10,000, ...).
    """
    cfg = config or MoneyManagementConfig()
    balance = max(0.0, balance)

    if cfg.mode == MoneyManagementMode.FIXED_AMOUNT:
        position_value = min(cfg.fixed_amount, balance)
        risk_amount = position_value * cfg.stop_loss_pct

    elif cfg.mode == MoneyManagementMode.PERCENTAGE_BALANCE:
        position_value = balance * cfg.percentage_balance
        risk_amount = position_value * cfg.stop_loss_pct

    elif cfg.mode in (
        MoneyManagementMode.RISK_PERCENTAGE,
        MoneyManagementMode.COMPOUNDING,
    ):
        # risk_amount   = account_balance * risk_per_trade
        # position_size = risk_amount / stop_loss_distance
        #
        # COMPOUNDING uses the exact same formula — since `balance` is
        # always the caller's *current* balance, realized profit/loss
        # is automatically folded into the next trade's sizing with no
        # extra bookkeeping required here.
        risk_amount = balance * cfg.risk_per_trade
        position_value = (
            risk_amount / cfg.stop_loss_pct if cfg.stop_loss_pct > 0 else 0.0
        )

    else:  # pragma: no cover - defensive, all Enum members handled above
        raise ValueError(f"Unknown money management mode: {cfg.mode!r}")

    # Never exceed available balance.
    position_value = min(position_value, balance)

    # Respect an optional portfolio-wide exposure cap.
    if cfg.max_position_pct_of_balance is not None:
        position_value = min(
            position_value, balance * cfg.max_position_pct_of_balance,
        )

    position_value = max(0.0, position_value)

    return PositionSizeResult(
        mode=cfg.mode.value,
        balance=balance,
        risk_amount=round(risk_amount, 8),
        position_value=round(position_value, 8),
        stop_loss_pct=cfg.stop_loss_pct,
        take_profit_pct=cfg.take_profit_pct,
    )


def stop_loss_price(entry_price: float, stop_loss_pct: float = STOP_LOSS_PCT) -> float:
    """Long-only stop-loss price at ``stop_loss_pct`` below entry."""
    return entry_price * (1.0 - stop_loss_pct)


def take_profit_price(entry_price: float, take_profit_pct: float = TAKE_PROFIT_PCT) -> float:
    """Long-only take-profit price at ``take_profit_pct`` above entry."""
    return entry_price * (1.0 + take_profit_pct)
