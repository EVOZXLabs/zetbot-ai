"""
ZetBot AI

Entry point — fetches live BTC/USDT data and displays
price, EMA200, RSI(14), ADX(14), +DI(14), -DI(14).
"""

import sys
from datetime import datetime

from bot.config import CONFIG
from bot.data import MarketData
from bot.logger import logger


def banner() -> None:
    print("=" * 45)
    print("          ZETBOT AI")
    print("=" * 45)
    print(f"Version : 0.1 Foundation")
    print(f"Status  : Starting")
    print(f"Time    : {datetime.now()}")
    print("=" * 45)


def main() -> None:
    banner()
    logger.info("Configuration loaded")

    try:
        md = MarketData(exchange_name=CONFIG["exchange"])
        symbol = CONFIG["symbol"]
        timeframe = CONFIG["timeframe"]

        df = md.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=250)

        price = float(df["close"].iloc[-1])
        ema200 = md.ema200(df)
        rsi14 = md.rsi(df)
        adx14 = md.adx(df)
        di_plus = md.plus_di(df)
        di_minus = md.minus_di(df)

        trend = "Bullish" if price > ema200 else "Bearish"

        print()
        print("=" * 45)
        print(" MARKET OVERVIEW")
        print("=" * 45)
        print(f" Symbol   : {symbol}")
        print(f" Timeframe: {timeframe}")
        print(f" Candles  : {len(df)}")
        print(f" Price    : ${price:,.2f}")
        print(f" EMA200   : ${ema200:,.2f}")
        print(f" RSI(14)  : {rsi14:.2f}")
        print(f" ADX(14)  : {adx14:.2f}")
        print(f" +DI(14)  : {di_plus:.2f}")
        print(f" -DI(14)  : {di_minus:.2f}")
        print(f" Trend    : {trend}")
        print("=" * 45)
        print()

        logger.info(
            "BTC/USDT price=%.2f EMA200=%.2f RSI=%.2f "
            "ADX=%.2f +DI=%.2f -DI=%.2f Trend=%s",
            price, ema200, rsi14, adx14, di_plus, di_minus, trend,
        )

    except Exception as e:
        logger.error("Fatal error: %s", e)
        print()
        print("ERROR")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
