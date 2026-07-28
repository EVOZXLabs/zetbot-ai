"""
Centralized configuration for the ZetBot AI orchestration pipeline.

All pipeline-relevant settings are consolidated here and loaded from
environment variables (with sensible defaults).

Usage::

    from scripts.app_config import load_config, AppConfig, validate_config
    config = load_config()
    validate_config(config)
"""

import os
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv


SUPPORTED_EXCHANGES: frozenset[str] = frozenset(
    {"binance", "bybit", "tokocrypto", "okx", "gate", "kucoin", "mexc", "indodax"}
)
VALID_TIMEFRAMES: frozenset[str] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}
)


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration."""

    # General
    paper_mode: bool = True
    exchange: str = "binance"
    timeframe: str = "1h"

    # Live trading credentials (required only when paper_mode is False)
    api_key: str = ""
    api_secret: str = ""
    quote_currency: str = "USDT"

    # Live protective orders (SL/TP) — off by default; a live BUY fill
    # will NOT get automatic protection orders unless explicitly enabled.
    auto_protect: bool = False
    default_stop_pct: float = 3.0
    default_take_profit_pct: float = 6.0
    protection_reconcile_interval_seconds: float = 8.0

    # Paths
    data_dir: str = "data"
    logs_dir: str = "logs"

    # Account
    account_balance: float = 10_000.0
    max_positions: int = 1
    max_risk_per_trade_pct: float = 1.0

    # Money Management (SPECIFICATION.md §25/§47) — RISK_PERCENTAGE is
    # the default production mode. Fractional notation (0.01 == 1%),
    # matching scripts/money_management.py, the single source of truth
    # for these defaults.
    money_management_mode: str = "RISK_PERCENTAGE"
    risk_per_trade: float = 0.01
    stop_loss_pct: float = 0.015
    take_profit_pct: float = 0.03
    daily_loss_limit: float = 0.03

    # Telegram
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout: int = 10
    telegram_retry: int = 3

    # Scanner
    scanner_threads: int = 5
    scanner_top_n: int = 50
    scanner_min_volume: float = 50_000.0

    # Decision engine
    decision_top_n: int = 20

    # Risk manager
    min_rr: float = 1.5
    max_rr: float = 5.0
    min_probability: float = 50.0
    max_atr_pct: float = 8.0
    min_volume_24h: float = 100_000.0
    stop_atr_multiplier: float = 1.5
    stop_fixed_pct: float = 5.0
    max_position_size_pct: float = 0.6

    # Position manager
    trail_atr_multiplier: float = 2.0
    max_holding_candles: int = 48
    tp1_sell_pct: float = 30.0
    tp2_sell_pct: float = 30.0
    tp3_sell_pct: float = 40.0

    # Safety limits
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_daily_trades: int = 20
    exchange_failure_window_seconds: int = 300
    exchange_max_failures: int = 3
    atr_spike_multiplier: float = 3.0

    # Paper trading
    taker_fee: float = 0.001
    maker_fee: float = 0.00075
    slippage_bps: int = 3

    # Pipeline scheduler
    auto_pipeline: bool = True
    pipeline_interval_seconds: int = 300

    # ------------------------------------------------------------------
    # On-chain / Web3 trading (DEX swaps on EVM chains + Solana)
    # ------------------------------------------------------------------
    onchain_enabled: bool = False
    onchain_chains: str = ""          # comma-separated: "ethereum,bsc,polygon,solana"
    onchain_slippage_bps: int = 50    # 0.50% default slippage tolerance
    onchain_live_confirmed: bool = False  # mirrors the CEX "CONFIRM LIVE" gate

    # EVM (used for ethereum/bsc/polygon/arbitrum/base — pick RPC per chain)
    evm_rpc_url: str = ""
    evm_wallet_address: str = ""
    evm_private_key: str = ""         # NEVER logged or sent over Telegram/WhatsApp

    # Solana
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_wallet_address: str = ""
    solana_private_key: str = ""      # base58-encoded secret key, NEVER logged

    # ------------------------------------------------------------------
    # WhatsApp control channel (via Twilio)
    # ------------------------------------------------------------------
    whatsapp_enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""    # e.g. "whatsapp:+14155238886"
    whatsapp_allowed_numbers: str = ""  # comma-separated allow-list, e.g. "whatsapp:+628123456789"
    whatsapp_webhook_host: str = "0.0.0.0"
    whatsapp_webhook_port: int = 8088


def load_config() -> AppConfig:
    """Load configuration from environment variables (via .env)."""
    load_dotenv()

    return AppConfig(
        paper_mode=os.getenv("PAPER_MODE", "true").lower() == "true",
        exchange=os.getenv("EXCHANGE", "binance"),
        timeframe=os.getenv("TIMEFRAME", "1h"),
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        quote_currency=os.getenv("QUOTE_CURRENCY", "USDT").upper(),
        auto_protect=os.getenv("AUTO_PROTECT", "false").lower() == "true",
        default_stop_pct=float(os.getenv("DEFAULT_STOP_PCT", "3.0")),
        default_take_profit_pct=float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "6.0")),
        protection_reconcile_interval_seconds=float(
            os.getenv("PROTECTION_RECONCILE_INTERVAL_SECONDS", "8.0"),
        ),
        data_dir=os.getenv("DATA_DIR", "data"),
        logs_dir=os.getenv("LOGS_DIR", "logs"),
        account_balance=float(os.getenv("ACCOUNT_BALANCE", "10000")),
        max_positions=int(os.getenv("MAX_POSITIONS", "1")),
        max_risk_per_trade_pct=float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.0")),
        money_management_mode=os.getenv("MONEY_MANAGEMENT_MODE", "RISK_PERCENTAGE"),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.01")),
        stop_loss_pct=float(os.getenv("MM_STOP_LOSS_PCT", "0.015")),
        take_profit_pct=float(os.getenv("MM_TAKE_PROFIT_PCT", "0.03")),
        daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", "0.03")),
        telegram_enabled=os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
        telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_timeout=int(os.getenv("TELEGRAM_TIMEOUT", "10")),
        telegram_retry=int(os.getenv("TELEGRAM_RETRY", "3")),
        scanner_threads=int(os.getenv("SCANNER_THREADS", "5")),
        scanner_top_n=int(os.getenv("SCANNER_TOP_N", "50")),
        scanner_min_volume=float(os.getenv("SCANNER_MIN_VOLUME", "50000")),
        decision_top_n=int(os.getenv("DECISION_TOP_N", "20")),
        min_rr=float(os.getenv("MIN_RR", "1.5")),
        max_rr=float(os.getenv("MAX_RR", "5.0")),
        min_probability=float(os.getenv("MIN_PROBABILITY", "50.0")),
        max_atr_pct=float(os.getenv("MAX_ATR_PCT", "8.0")),
        min_volume_24h=float(os.getenv("MIN_VOLUME_24H", "100000")),
        stop_atr_multiplier=float(os.getenv("STOP_ATR_MULTIPLIER", "1.5")),
        stop_fixed_pct=float(os.getenv("STOP_FIXED_PCT", "5.0")),
        max_position_size_pct=float(os.getenv("MAX_POSITION_SIZE_PCT", "0.6")),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0")),
        max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "20")),
        exchange_failure_window_seconds=int(os.getenv("EXCHANGE_FAILURE_WINDOW_SECONDS", "300")),
        exchange_max_failures=int(os.getenv("EXCHANGE_MAX_FAILURES", "3")),
        atr_spike_multiplier=float(os.getenv("ATR_SPIKE_MULTIPLIER", "3.0")),
        trail_atr_multiplier=float(os.getenv("TRAIL_ATR_MULTIPLIER", "2.0")),
        max_holding_candles=int(os.getenv("MAX_HOLDING_CANDLES", "48")),
        tp1_sell_pct=float(os.getenv("TP1_SELL_PCT", "30.0")),
        tp2_sell_pct=float(os.getenv("TP2_SELL_PCT", "30.0")),
        tp3_sell_pct=float(os.getenv("TP3_SELL_PCT", "40.0")),
        taker_fee=float(os.getenv("TAKER_FEE", "0.001")),
        maker_fee=float(os.getenv("MAKER_FEE", "0.00075")),
        slippage_bps=int(os.getenv("SLIPPAGE_BPS", "3")),
        auto_pipeline=os.getenv("AUTO_PIPELINE", "true").lower() == "true",
        pipeline_interval_seconds=int(os.getenv("PIPELINE_INTERVAL", "300")),
        onchain_enabled=os.getenv("ONCHAIN_ENABLED", "false").lower() == "true",
        onchain_chains=os.getenv("ONCHAIN_CHAINS", ""),
        onchain_slippage_bps=int(os.getenv("ONCHAIN_SLIPPAGE_BPS", "50")),
        onchain_live_confirmed=os.getenv("ONCHAIN_LIVE_CONFIRMED", "false").lower() == "true",
        evm_rpc_url=os.getenv("EVM_RPC_URL", ""),
        evm_wallet_address=os.getenv("EVM_WALLET_ADDRESS", ""),
        evm_private_key=os.getenv("EVM_PRIVATE_KEY", ""),
        solana_rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        solana_wallet_address=os.getenv("SOLANA_WALLET_ADDRESS", ""),
        solana_private_key=os.getenv("SOLANA_PRIVATE_KEY", ""),
        whatsapp_enabled=os.getenv("WHATSAPP_ENABLED", "false").lower() == "true",
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
        twilio_whatsapp_from=os.getenv("TWILIO_WHATSAPP_FROM", ""),
        whatsapp_allowed_numbers=os.getenv("WHATSAPP_ALLOWED_NUMBERS", ""),
        whatsapp_webhook_host=os.getenv("WHATSAPP_WEBHOOK_HOST", "0.0.0.0"),
        whatsapp_webhook_port=int(os.getenv("WHATSAPP_WEBHOOK_PORT", "8088")),
    )


class ConfigError(Exception):
    """Raised when configuration validation fails."""


def validate_config(config: AppConfig) -> None:
    """Validate all configuration values.

    Raises ``ConfigError`` (which prints to stderr and exits) if any
    required value is missing, invalid, or out of range.
    """
    errors: list[str] = []

    if config.account_balance <= 0:
        errors.append(f"ACCOUNT_BALANCE must be > 0 (got {config.account_balance})")
    if config.exchange not in SUPPORTED_EXCHANGES:
        errors.append(
            f"EXCHANGE must be one of {sorted(SUPPORTED_EXCHANGES)} "
            f"(got '{config.exchange}')"
        )
    if config.timeframe not in VALID_TIMEFRAMES:
        errors.append(
            f"TIMEFRAME must be one of {sorted(VALID_TIMEFRAMES)} "
            f"(got '{config.timeframe}')"
        )
    if config.max_positions < 1:
        errors.append(f"MAX_POSITIONS must be >= 1 (got {config.max_positions})")
    if config.max_risk_per_trade_pct <= 0 or config.max_risk_per_trade_pct > 100:
        errors.append(
            f"MAX_RISK_PER_TRADE_PCT must be between 0 and 100 "
            f"(got {config.max_risk_per_trade_pct})"
        )
    if config.scanner_threads < 1:
        errors.append(f"SCANNER_THREADS must be >= 1 (got {config.scanner_threads})")
    if config.scanner_top_n < 1:
        errors.append(f"SCANNER_TOP_N must be >= 1 (got {config.scanner_top_n})")
    if config.telegram_timeout < 1:
        errors.append(
            f"TELEGRAM_TIMEOUT must be >= 1 (got {config.telegram_timeout})"
        )
    if config.telegram_retry < 0:
        errors.append(
            f"TELEGRAM_RETRY must be >= 0 (got {config.telegram_retry})"
        )
    if config.min_rr <= 0 or config.max_rr <= config.min_rr:
        errors.append(
            f"MIN_RR ({config.min_rr}) must be positive and less than "
            f"MAX_RR ({config.max_rr})"
        )
    if config.min_probability < 0 or config.min_probability > 100:
        errors.append(f"MIN_PROBABILITY must be 0-100 (got {config.min_probability})")
    if config.max_atr_pct <= 0:
        errors.append(f"MAX_ATR_PCT must be > 0 (got {config.max_atr_pct})")
    if config.max_holding_candles < 1:
        errors.append(
            f"MAX_HOLDING_CANDLES must be >= 1 (got {config.max_holding_candles})"
        )
    tp_sum = config.tp1_sell_pct + config.tp2_sell_pct + config.tp3_sell_pct
    if abs(tp_sum - 100.0) > 0.01:
        errors.append(
            f"TP sell percentages must sum to 100 (got "
            f"{config.tp1_sell_pct}+{config.tp2_sell_pct}+"
            f"{config.tp3_sell_pct}={tp_sum})"
        )
    if config.taker_fee < 0 or config.maker_fee < 0:
        errors.append("TAKER_FEE and MAKER_FEE must be non-negative")
    if config.slippage_bps < 0:
        errors.append(f"SLIPPAGE_BPS must be >= 0 (got {config.slippage_bps})")
    valid_mm_modes = {"FIXED_AMOUNT", "PERCENTAGE_BALANCE", "RISK_PERCENTAGE", "COMPOUNDING"}
    if config.money_management_mode not in valid_mm_modes:
        errors.append(
            f"MONEY_MANAGEMENT_MODE must be one of {sorted(valid_mm_modes)} "
            f"(got '{config.money_management_mode}')"
        )
    if config.risk_per_trade <= 0 or config.risk_per_trade > 1.0:
        errors.append(
            f"RISK_PER_TRADE must be between 0 and 1.0 (got {config.risk_per_trade})"
        )
    if config.stop_loss_pct <= 0 or config.stop_loss_pct > 1.0:
        errors.append(
            f"MM_STOP_LOSS_PCT must be between 0 and 1.0 (got {config.stop_loss_pct})"
        )
    if config.take_profit_pct <= 0 or config.take_profit_pct > 1.0:
        errors.append(
            f"MM_TAKE_PROFIT_PCT must be between 0 and 1.0 (got {config.take_profit_pct})"
        )
    if config.daily_loss_limit <= 0 or config.daily_loss_limit > 1.0:
        errors.append(
            f"DAILY_LOSS_LIMIT must be between 0 and 1.0 (got {config.daily_loss_limit})"
        )

    if not config.paper_mode and (not config.api_key or not config.api_secret):
        errors.append(
            "PAPER_MODE=false requires API_KEY and API_SECRET to be set "
            "(live trading cannot start without exchange credentials)"
        )

    if errors:
        raise ConfigError("\n".join(errors))
