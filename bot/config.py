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

    "symbol": os.getenv("SYMBOL", "BTC/USDT"),

    "timeframe": os.getenv("TIMEFRAME", "1h"),

    "position_size": float(os.getenv("POSITION_SIZE", 10)),

    "stop_loss": float(os.getenv("STOP_LOSS", 1.5)),

    "take_profit": float(os.getenv("TAKE_PROFIT", 2.5)),

    "adx_threshold": float(os.getenv("ADX_THRESHOLD", 25)),

    "atr_multiplier": float(os.getenv("ATR_MULTIPLIER", 0.5)),

    "volatility_lookback": int(os.getenv("VOLATILITY_LOOKBACK", 14)),

    "price_compression_lookback": int(os.getenv("PRICE_COMPRESSION_LOOKBACK", 20)),

    "compression_ratio": float(os.getenv("COMPRESSION_RATIO", 0.3)),

}
