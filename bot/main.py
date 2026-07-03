"""
====================================

ZetBot AI

Version 0.1 Foundation

====================================
"""

import sys
from datetime import datetime

import ccxt

from bot.exchange import Exchange


def banner():

    print("=" * 40)
    print("         ZETBOT AI")
    print("=" * 40)

    print("Version : 0.1 Foundation")

    print("Status  : Starting")

    print("Time    :", datetime.now())

    print("=" * 40)


def main():

    banner()

    from bot.config import CONFIG

    from bot.logger import logger

    logger.info("Configuration Loaded")

    logger.info("Logger Loaded")

    logger.info("Exchange Loaded")

    print()

    print("Exchange :", CONFIG["exchange"])

    print("Symbol   :", CONFIG["symbol"])

    print()

    try:

        ex = Exchange()

        price = ex.last_price()

        print()

        print("BTC PRICE")

        print(price)

        logger.info("Price Loaded")

    except ccxt.NetworkError as e:

        logger.error(f"Network error: {e}")

        print()

        print("CONNECTION ERROR")

        print("Could not reach Binance. Check your internet connection.")

        sys.exit(1)

    except ccxt.ExchangeError as e:

        logger.error(f"Exchange error: {e}")

        print()

        print("EXCHANGE ERROR")

        print(e)

        sys.exit(1)

    except Exception as e:

        logger.error(str(e))

        print()

        print("ERROR")

        print(e)

        sys.exit(1)


if __name__ == "__main__":
    main()
