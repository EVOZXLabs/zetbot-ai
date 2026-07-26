"""
====================================

ZetBot AI
Configuration

====================================
"""

import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {

    "exchange": os.getenv("EXCHANGE", "binance"),

    "api_key": os.getenv("API_KEY", ""),

    "api_secret": os.getenv("API_SECRET", ""),

    "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),

    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),

    "telegram_enabled": os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",

    "telegram_timeout": int(os.getenv("TELEGRAM_TIMEOUT", 10)),

    "telegram_retry": int(os.getenv("TELEGRAM_RETRY", 3)),

    "symbol": os.getenv("SYMBOL", "BTC/USDT"),

    "timeframe": os.getenv("TIMEFRAME", "1h"),

    "position_size": float(os.getenv("POSITION_SIZE", 10)),

    # Fixed-% fallbacks — only used when ATR data is unavailable
    # (e.g. not enough candles yet). Live/paper trading normally uses
    # the ATR-based dynamic stop/target below instead of these.

    "stop_loss": float(os.getenv("STOP_LOSS", 1.5)),

    "take_profit": float(os.getenv("TAKE_PROFIT", 2.5)),

    # ---- Dynamic risk engine (equity- and volatility-aware) --------

    # Stop distance = ATR% * ATR_STOP_MULTIPLIER. Bigger multiplier =
    # wider stop = fewer premature stop-outs, but bigger $ risk per trade.
    "atr_stop_multiplier": float(os.getenv("ATR_STOP_MULTIPLIER", 1.5)),

    # Take-profit distance = stop distance * RISK_REWARD_RATIO, so the
    # target always scales with current volatility instead of being a
    # flat %.
    "risk_reward_ratio": float(os.getenv("RISK_REWARD_RATIO", 2.0)),

    # ATR lookback period used for the dynamic stop/target calculation.
    "atr_period": int(os.getenv("ATR_PERIOD", 14)),

    # Absolute dollar floor for a single position. Reflects real
    # exchange minimum-notional rules (e.g. Binance spot ~$5-10 USDT) —
    # accounts too small to clear this floor at the configured position
    # size % will correctly have trades rejected rather than placing
    # unfillable orders.
    "min_position_usd": float(os.getenv("MIN_POSITION_USD", 10.0)),


    "adx_threshold": float(os.getenv("ADX_THRESHOLD", 25)),

    "atr_multiplier": float(os.getenv("ATR_MULTIPLIER", 0.5)),

    "volatility_lookback": int(os.getenv("VOLATILITY_LOOKBACK", 14)),

    "price_compression_lookback": int(os.getenv("PRICE_COMPRESSION_LOOKBACK", 20)),

    "compression_ratio": float(os.getenv("COMPRESSION_RATIO", 0.3)),

    "loop_enabled": os.getenv("LOOP_ENABLED", "true").lower() == "true",

    "loop_interval_seconds": int(os.getenv("LOOP_INTERVAL_SECONDS", 60)),

    "max_retry": int(os.getenv("MAX_RETRY", 3)),

    "retry_delay": int(os.getenv("RETRY_DELAY", 5)),

    "state_enabled": os.getenv("STATE_ENABLED", "true").lower() == "true",

    "state_path": os.getenv("STATE_PATH", "data/state.json"),

    "auto_save": os.getenv("AUTO_SAVE", "true").lower() == "true",

    "backup_corrupted_state": os.getenv("BACKUP_CORRUPTED_STATE", "true").lower() == "true",

    "testing": os.getenv("TESTING", "false").lower() == "true",

}
