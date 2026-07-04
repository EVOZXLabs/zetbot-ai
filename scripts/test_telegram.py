#!/usr/bin/env python3
"""
Telegram Connectivity Test

Loads environment variables, initialises the TelegramNotifier,
and sends a single test message to verify the connection.

Usage:
    python scripts/test_telegram.py

Environment variables (from .env or shell):
    TELEGRAM_ENABLED=true
    TELEGRAM_TOKEN=<your_bot_token>
    TELEGRAM_CHAT_ID=<your_chat_id>
"""

import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that 'bot' is importable
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv

load_dotenv()

from bot.config import CONFIG
from bot.telegram import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

if not CONFIG.get("telegram_enabled", False):
    print("Telegram is disabled. Set TELEGRAM_ENABLED=true and provide TELEGRAM_TOKEN + TELEGRAM_CHAT_ID.")
    sys.exit(1)

if not CONFIG.get("telegram_token") or not CONFIG.get("telegram_chat_id"):
    print("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
    sys.exit(1)

notifier = TelegramNotifier()

if not notifier._enabled:
    print("TelegramNotifier initialisation failed — check credentials.")
    sys.exit(1)

success = notifier._send("\U0001f680 ZetBot AI Connected\nPaper Trading Mode")

if success:
    print("Telegram connectivity test PASSED — message sent.")
else:
    print("Telegram connectivity test FAILED — message could not be sent.")
    sys.exit(1)
