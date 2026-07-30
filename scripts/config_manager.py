"""
Configuration Manager for ZetBot AI.

Handles .env file reading, writing, validation, and display.
"""

import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

from dotenv import load_dotenv, set_key, unset_key

from scripts.app_config import (
    AppConfig,
    ConfigError,
    SUPPORTED_EXCHANGES,
    VALID_TIMEFRAMES,
    load_config,
    validate_config,
)


ENV_PATH = ".env"
ENV_EXAMPLE_PATH = ".env.example"
MASK = "******"


@dataclass
class ConfigField:
    key: str
    label: str
    default: str
    secret: bool = False
    required: bool = True
    validator: Optional[callable] = None


CONFIG_FIELDS: list[ConfigField] = [
    ConfigField("EXCHANGE", "Exchange", "binance",
                validator=lambda v: v.lower() in SUPPORTED_EXCHANGES),
    ConfigField("API_KEY", "API Key", "", secret=True),
    ConfigField("API_SECRET", "API Secret", "", secret=True),
    ConfigField("PAPER_MODE", "Paper Mode (true/false)", "true",
                validator=lambda v: v.lower() in ("true", "false")),
    ConfigField("TELEGRAM_ENABLED", "Telegram Enabled (true/false)", "false",
                validator=lambda v: v.lower() in ("true", "false")),
    ConfigField("TELEGRAM_TOKEN", "Telegram Bot Token", "", secret=True),
    ConfigField("TELEGRAM_CHAT_ID", "Telegram Chat ID", "", secret=True),
    ConfigField("POSITION_SIZE", "Position Size (Quote Currency)", "10"),
    ConfigField("MAX_POSITIONS", "Max Open Positions", "1"),
    ConfigField("TIMEFRAME", "Timeframe", "1h",
                validator=lambda v: v in VALID_TIMEFRAMES),
    ConfigField("STOP_LOSS_PCT", "Stop Loss (%)", "1.5"),
    ConfigField("TAKE_PROFIT_PCT", "Take Profit (%)", "3.0"),
    ConfigField("AUTO_PIPELINE", "Auto Pipeline (true/false)", "true",
                validator=lambda v: v.lower() in ("true", "false")),
    ConfigField("PIPELINE_INTERVAL", "Pipeline Interval (seconds)", "300"),
    ConfigField("ACCOUNT_BALANCE", "Initial Account Balance (Quote Currency)", "10000"),
    ConfigField("MAX_RISK_PER_TRADE_PCT", "Max Risk Per Trade (%)", "1.0"),
    ConfigField("MIN_RR", "Min Risk/Reward Ratio", "1.5"),
    ConfigField("MAX_RR", "Max Risk/Reward Ratio", "5.0"),
    ConfigField("SCANNER_THREADS", "Scanner Threads", "5"),
    ConfigField("SCANNER_TOP_N", "Scanner Top N", "50"),
    ConfigField("SCANNER_MIN_VOLUME", "Scanner Min Volume (Quote Currency)", "50000"),
    # Money Management (SPECIFICATION.md §25/§47) — default production
    # mode is RISK_PERCENTAGE. Fractional notation (0.01 == 1%).
    ConfigField("MONEY_MANAGEMENT_MODE", "Money Management Mode", "RISK_PERCENTAGE",
                validator=lambda v: v.upper() in (
                    "FIXED_AMOUNT", "PERCENTAGE_BALANCE",
                    "RISK_PERCENTAGE", "COMPOUNDING",
                )),
    ConfigField("RISK_PER_TRADE", "Risk Per Trade (fraction)", "0.01"),
    ConfigField("MM_STOP_LOSS_PCT", "Money Mgmt Stop Loss (fraction)", "0.015"),
    ConfigField("MM_TAKE_PROFIT_PCT", "Money Mgmt Take Profit (fraction)", "0.03"),
    ConfigField("DAILY_LOSS_LIMIT", "Daily Loss Limit (fraction)", "0.03"),
]


def env_exists() -> bool:
    return os.path.exists(ENV_PATH)


def env_is_valid() -> bool:
    try:
        cfg = load_config()
        validate_config(cfg)
        if not cfg.exchange:
            return False
        return True
    except (ConfigError, Exception):
        return False


def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not env_exists():
        return result
    load_dotenv(ENV_PATH, override=True)
    for field in CONFIG_FIELDS:
        value = os.getenv(field.key, "")
        result[field.key] = value
    return result


def write_env(values: dict[str, str], path: str = ENV_PATH) -> None:
    for key, value in values.items():
        set_key(path, key, value)


def display_config() -> str:
    lines = ["\n=== Current Configuration ===\n"]
    for field in CONFIG_FIELDS:
        value = os.getenv(field.key, field.default)
        display = MASK if field.secret and value else (value or "(not set)")
        lines.append(f"  {field.label:30s} = {display}")
    return "\n".join(lines)


def config_to_dict() -> dict[str, str]:
    result: dict[str, str] = {}
    for field in CONFIG_FIELDS:
        value = os.getenv(field.key, field.default)
        result[field.key] = value
    return result


def env_as_dict() -> dict[str, str]:
    return config_to_dict()


def validate_env_dict(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for field in CONFIG_FIELDS:
        value = values.get(field.key, "").strip()
        if field.required and not value:
            errors.append(f"{field.label} is required")
            continue
        if value and field.validator:
            if not field.validator(value):
                errors.append(f"{field.label}: invalid value '{value}'")
    return errors


def reset_env(backup: bool = True) -> None:
    if backup and env_exists():
        import shutil
        backup_path = f"{ENV_PATH}.backup.{int(__import__('time').time())}"
        shutil.copy2(ENV_PATH, backup_path)
    if os.path.exists(ENV_PATH):
        os.remove(ENV_PATH)
