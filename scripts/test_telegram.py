#!/usr/bin/env python3
"""
Telegram Connectivity Test

Loads environment variables, initialises the TelegramNotifier,
and sends a single test message to verify the connection.

Manual usage:
    python scripts/test_telegram.py

Environment variables (from .env or shell):
    TELEGRAM_ENABLED=true
    TELEGRAM_TOKEN=<your_bot_token>
    TELEGRAM_CHAT_ID=<your_chat_id>

This module is also safe to import under pytest: importing it performs
no network calls, no configuration validation, and no sys.exit() calls.
All of that logic lives inside main(), which only runs when the script
is executed directly (``if __name__ == "__main__"``).
"""

import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that 'bot' is importable
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv

from bot.config import CONFIG
from bot.telegram import TelegramNotifier


def run_connectivity_test() -> bool:
    """Run the manual Telegram connectivity check.

    Prints human-readable status messages and returns True on success,
    False on any failure (missing config, disabled notifier, or a
    failed send). Does not call sys.exit() itself so it can be reused
    programmatically; the __main__ block below translates the result
    into a process exit code.
    """
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not CONFIG.get("telegram_enabled", False):
        print("Telegram is disabled. Set TELEGRAM_ENABLED=true and provide TELEGRAM_TOKEN + TELEGRAM_CHAT_ID.")
        return False

    if not CONFIG.get("telegram_token") or not CONFIG.get("telegram_chat_id"):
        print("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
        return False

    notifier = TelegramNotifier()

    if not notifier._enabled:
        print("TelegramNotifier initialisation failed — check credentials.")
        return False

    success = notifier._send("\U0001f680 ZetBot AI Connected\nPaper Trading Mode")

    if success:
        print("Telegram connectivity test PASSED — message sent.")
    else:
        print("Telegram connectivity test FAILED — message could not be sent.")

    return success


# ---------------------------------------------------------------------------
# Pytest-collectible tests
#
# These run safely in any environment (no network access, no live
# credentials required) and verify that this script's supporting pieces
# are importable and well-formed, without triggering the manual CLI flow.
# ---------------------------------------------------------------------------

def test_module_imports_without_side_effects():
    """Importing this module must not raise or exit the interpreter."""
    assert callable(run_connectivity_test)


def test_config_has_telegram_keys():
    """The shared CONFIG dict must expose the keys this script depends on."""
    for key in ("telegram_enabled", "telegram_token", "telegram_chat_id"):
        assert key in CONFIG


def test_telegram_notifier_instantiates():
    """TelegramNotifier must be constructible regardless of config state."""
    notifier = TelegramNotifier()
    assert hasattr(notifier, "_enabled")
    assert isinstance(notifier._enabled, bool)


if __name__ == "__main__":
    _ok = run_connectivity_test()
    sys.exit(0 if _ok else 1)
