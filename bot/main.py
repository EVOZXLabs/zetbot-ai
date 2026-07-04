"""
ZetBot AI

Entry point — executes a continuous 24/7 paper trading loop.

Usage::

    python -m bot.main
"""

import sys
from datetime import datetime

from bot.config import CONFIG
from bot.loop import TradingLoop
from bot.logger import logger


def banner() -> None:
    print("=" * 50)
    print("          ZETBOT AI")
    print("=" * 50)
    print(f"Version : 0.2 Continuous Paper Trading")
    print(f"Status  : Starting")
    print(f"Time    : {datetime.now()}")
    print("=" * 50)


def main() -> None:
    banner()
    logger.info("Configuration loaded")

    try:
        loop = TradingLoop()
        loop.run()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.error("Fatal error: %s", e)
        print()
        print("ERROR")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
