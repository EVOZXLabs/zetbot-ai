"""
Centralized configuration for the ZetBot AI orchestration pipeline.

All pipeline-relevant settings are consolidated here and loaded from
environment variables (with sensible defaults).

Usage::

    from scripts.app_config import load_config, AppConfig
    config = load_config()
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration."""

    # General
    paper_mode: bool = True
    exchange: str = "binance"
    timeframe: str = "1h"

    # Paths
    data_dir: str = "data"
    logs_dir: str = "logs"

    # Account
    account_balance: float = 10_000.0
    max_positions: int = 3
    max_risk_per_trade_pct: float = 2.0

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

    # Position manager
    trail_atr_multiplier: float = 2.0
    max_holding_candles: int = 48
    tp1_sell_pct: float = 30.0
    tp2_sell_pct: float = 30.0
    tp3_sell_pct: float = 40.0

    # Paper trading
    taker_fee: float = 0.001
    maker_fee: float = 0.00075
    slippage_bps: int = 3


def load_config() -> AppConfig:
    """Load configuration from environment variables (via .env)."""
    load_dotenv()

    return AppConfig(
        paper_mode=os.getenv("PAPER_MODE", "true").lower() == "true",
        exchange=os.getenv("EXCHANGE", "binance"),
        timeframe=os.getenv("TIMEFRAME", "1h"),
        data_dir=os.getenv("DATA_DIR", "data"),
        logs_dir=os.getenv("LOGS_DIR", "logs"),
        account_balance=float(os.getenv("ACCOUNT_BALANCE", "10000")),
        max_positions=int(os.getenv("MAX_POSITIONS", "3")),
        max_risk_per_trade_pct=float(os.getenv("MAX_RISK_PER_TRADE_PCT", "2.0")),
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
        trail_atr_multiplier=float(os.getenv("TRAIL_ATR_MULTIPLIER", "2.0")),
        max_holding_candles=int(os.getenv("MAX_HOLDING_CANDLES", "48")),
        tp1_sell_pct=float(os.getenv("TP1_SELL_PCT", "30.0")),
        tp2_sell_pct=float(os.getenv("TP2_SELL_PCT", "30.0")),
        tp3_sell_pct=float(os.getenv("TP3_SELL_PCT", "40.0")),
        taker_fee=float(os.getenv("TAKER_FEE", "0.001")),
        maker_fee=float(os.getenv("MAKER_FEE", "0.00075")),
        slippage_bps=int(os.getenv("SLIPPAGE_BPS", "3")),
    )
