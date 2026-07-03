"""
====================================

Exchange Module

ZetBot AI

====================================
"""

import ccxt

from bot.config import CONFIG


class Exchange:

    def __init__(self):

        exchange_name = CONFIG["exchange"].lower()

        if exchange_name == "binance":

            self.exchange = ccxt.binance({
                "apiKey": CONFIG["api_key"],
                "secret": CONFIG["api_secret"],
                "enableRateLimit": True
            })

        elif exchange_name == "bybit":

            self.exchange = ccxt.bybit({
                "apiKey": CONFIG["api_key"],
                "secret": CONFIG["api_secret"],
                "enableRateLimit": True
            })

        elif exchange_name == "tokocrypto":

            self.exchange = ccxt.binance({
                "apiKey": CONFIG["api_key"],
                "secret": CONFIG["api_secret"],
                "enableRateLimit": True
            })

        else:

            msg = f"Unsupported exchange: {exchange_name}"
            raise ValueError(msg)

    def ticker(self):

        return self.exchange.fetch_ticker(
            CONFIG["symbol"]
        )

    def last_price(self):

        ticker = self.ticker()

        return ticker["last"]

    def balance(self):

        return self.exchange.fetch_balance()
