"""
ZetBot AI

Entry point — fetches live BTC/USDT data and displays
price, EMA200, RSI(14), ADX(14), ATR(14), market state,
trading signal, position status, trade result,
and reason breakdown.
"""

import sys
from datetime import datetime

from bot.config import CONFIG
from bot.data import MarketData
from bot.logger import logger
from bot.paper import PaperTrader
from bot.strategy import BUY, SELL, StrategyEngine


def _print_trade(trade: dict) -> None:
    direction = "LONG"
    pnl = trade["net_pnl"]
    pnl_str = f"${pnl:+,.2f}"
    print(f" Trade     : {direction}")
    print(f" Entry     : ${trade['entry_price']:,.2f}")
    print(f" Exit      : ${trade['exit_price']:,.2f}")
    print(f" PnL       : {pnl_str} ({trade['pnl_pct']:+.2f}%)")
    print(f" Exit      : {trade['exit_reason']}")
    print(f" Balance   : ${trade['balance_after']:,.2f}")


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

        paper = PaperTrader()

        result = engine.evaluate(df, has_position=paper.has_position())
        signal = result["signal"]
        reasons = result["reason"]

        position = paper.current_position()

        if position:
            if price >= position["take_profit_price"]:
                trade = paper.close_position(price, "Take Profit")
            elif price <= position["stop_loss_price"]:
                trade = paper.close_position(price, "Stop Loss")
            elif signal == SELL:
                trade = paper.close_position(price, "Strategy Exit")
            else:
                trade = None
        else:
            trade = None

        if signal == BUY:
            paper.open_position(
                entry_price=price,
                symbol=symbol,
                timeframe=timeframe,
                reasons=reasons,
            )

        position = paper.current_position()

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
        print("-" * 45)
        if trade:
            print(" TRADE RESULT")
            print("-" * 45)
            _print_trade(trade)
            print("-" * 45)
        print(" PAPER POSITION")
        print("-" * 45)
        if position:
            print(f" Entry      : ${position['entry_price']:,.2f}")
            print(f" Stop Loss  : ${position['stop_loss_price']:,.2f}")
            print(f" Take Profit: ${position['take_profit_price']:,.2f}")
            pct = position["position_size_percent"]
            print(f" Size       : {pct}%")
        else:
            print(" No Paper Position")
        print("=" * 45)
        print()

        pos_flag = "YES" if position else "NO"
        logger.info(
            "BTC/USDT price=%.2f EMA200=%.2f RSI=%.2f "
            "ADX=%.2f ATR=%.2f Market=%s Signal=%s Position=%s",
            price, ema200, rsi14, adx14, atr14, market, signal, pos_flag,
        )

    except Exception as e:
        logger.error("Fatal error: %s", e)
        print()
        print("ERROR")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
