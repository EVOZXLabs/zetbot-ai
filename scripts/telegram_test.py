"""
Telegram Connection Test for ZetBot AI.

Sends a test message to verify the bot can communicate via Telegram.
"""

import sys


def run_telegram_test() -> str:
    """Test Telegram connectivity. Returns a formatted report string."""
    try:
        from scripts.app_config import load_config
        config = load_config()
    except Exception as exc:
        return f"Failed to load config: {exc}"

    if not config.telegram_enabled:
        return "Telegram is disabled in configuration."

    if not config.telegram_token or not config.telegram_chat_id:
        return "Telegram token or chat ID not configured."

    try:
        from bot.telegram import TelegramNotifier
        import bot.config as bot_cfg

        bot_cfg.CONFIG.update({
            "telegram_enabled": True,
            "telegram_token": config.telegram_token,
            "telegram_chat_id": config.telegram_chat_id,
            "telegram_timeout": config.telegram_timeout,
            "telegram_retry": config.telegram_retry,
        })

        notifier = TelegramNotifier()
        msg = "\u2705 ZetBot AI Connected Successfully"
        notifier.send(msg)
        return f"Test message sent to chat {config.telegram_chat_id}."

    except Exception as exc:
        return f"Telegram test FAILED: {exc}"
