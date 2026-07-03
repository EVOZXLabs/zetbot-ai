"""
====================================

ZetBot AI

Version 0.1 Foundation

====================================
"""

from datetime import datetime


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

    print()

    print("Exchange :", CONFIG["exchange"])

    print("Symbol   :", CONFIG["symbol"])

    print("Timeframe:", CONFIG["timeframe"])

    print()

    print("Foundation Ready.")
