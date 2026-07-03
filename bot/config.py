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

    "take_profit": float(os.getenv("TAKE_PROFIT", 2.5))

}
