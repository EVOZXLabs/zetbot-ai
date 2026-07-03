"""
====================================

ZetBot AI

Version 0.1 Foundation

====================================
"""

from datetime import datetime

from exchange import Exchange


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

    from config import CONFIG

    from logger import logger

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

    except Exception as e:

        logger.error(str(e))

        print()

        print("ERROR")

        print(e)
