"""
ZetBot AI

Entry point — fetches live BTC/USDT data and displays
price, EMA200, RSI(14), ADX(14), ATR(14), market state,
trading signal, and reason breakdown.
"""

import sys
from datetime import datetime

from bot.config import CONFIG
from bot.data import MarketData
from bot.logger import logger
from bot.strategy import StrategyEngine


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
        atr14 = md.atr(df)
        market = md.market_state(df)

        engine = StrategyEngine(
            adx_threshold=CONFIG.get("adx_threshold", 25),
            atr_multiplier=CONFIG.get("atr_multiplier", 0.5),
            volatility_period=CONFIG.get("volatility_lookback", 14),
            compression_lookback=CONFIG.get("price_compression_lookback", 20),
            compression_ratio=CONFIG.get("compression_ratio", 0.3),
        )
        result = engine.evaluate(df, has_position=False)
        signal = result["signal"]
        reasons = result["reason"]

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
        print(f" ATR(14)  : ${atr14:,.2f}")
        print(f" Market   : {market}")
        print(f" Signal   : {signal}")
        for r in reasons:
            print(f"            - {r}")
        print("=" * 45)
        print()

        logger.info(
            "BTC/USDT price=%.2f EMA200=%.2f RSI=%.2f "
            "ADX=%.2f ATR=%.2f Market=%s Signal=%s",
            price, ema200, rsi14, adx14, atr14, market, signal,
        )

    except Exception as e:
        logger.error("Fatal error: %s", e)
        print()
        print("ERROR")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
