"""
ZetBot AI

Entry point — executes one complete paper trading cycle and displays
market data, trading signal, position status, trade result,
and statistics.
"""

import sys
from datetime import datetime

from bot.config import CONFIG
from bot.logger import logger
from bot.paper_engine import PaperTradingEngine


def _print_trade(trade: dict) -> None:
    pnl = trade["net_pnl"]
    pnl_str = f"${pnl:+,.2f}"
    print(f" Entry     : ${trade['entry_price']:,.2f}")
    print(f" Exit      : ${trade['exit_price']:,.2f}")
    print(f" PnL       : {pnl_str} ({trade['pnl_pct']:+.2f}%)")
    print(f" Exit      : {trade['exit_reason']}")
    print(f" Balance   : ${trade['balance_after']:,.2f}")


def _print_stats(stats: dict) -> None:
    print(f" Total Trades  : {stats['total_trades']}")
    print(f" Total Profit  : ${stats['total_profit']:+,.2f}")
    print(f" Win Rate      : {stats['win_rate']:.1f}%")
    print(f" Profit Factor : {stats['profit_factor']:.2f}")
    print(f" Avg Win       : ${stats['average_win']:+,.2f}")
    print(f" Avg Loss      : ${stats['average_loss']:+,.2f}")
    print(f" Largest Win   : ${stats['largest_win']:+,.2f}")
    print(f" Largest Loss  : ${stats['largest_loss']:+,.2f}")
    print(f" Win Streak    : {stats['longest_win_streak']}")
    print(f" Loss Streak   : {stats['longest_loss_streak']}")
    print(f" Avg Hold      : {stats['average_holding_time']}")


def banner() -> None:
    print("=" * 50)
    print("          ZETBOT AI")
    print("=" * 50)
    print(f"Version : 0.1 Complete Paper Trading")
    print(f"Status  : Starting")
    print(f"Time    : {datetime.now()}")
    print("=" * 50)


def main() -> None:
    banner()
    logger.info("Configuration loaded")

    try:
        engine = PaperTradingEngine()

        result = engine.run_once()

        symbol = CONFIG.get("symbol", "BTC/USDT")
        timeframe = CONFIG.get("timeframe", "1h")
        signal = result["signal"]
        trade = result["trade"]
        position = result["position"]
        price = result["price"]
        market_state = result["market_state"]
        balance = engine.current_balance()
        stats = engine.statistics()

        print()
        print("=" * 50)
        print(" MARKET OVERVIEW")
        print("=" * 50)
        print(f" Symbol      : {symbol}")
        print(f" Timeframe   : {timeframe}")
        print(f" Price       : ${price:,.2f}")
        print(f" Market      : {market_state}")
        print(f" Balance     : ${balance:,.2f}")
        if signal:
            s = signal.get("signal", "?")
            print(f" Signal      : {s}")
            for r in signal.get("reason", []):
                print(f"              - {r}")
        print("-" * 50)
        if trade:
            print(" TRADE RESULT")
            print("-" * 50)
            _print_trade(trade)
            print("-" * 50)
        print(" PAPER POSITION")
        print("-" * 50)
        if position:
            print(f" Entry       : ${position['entry_price']:,.2f}")
            print(f" Stop Loss   : ${position['stop_loss_price']:,.2f}")
            print(f" Take Profit : ${position['take_profit_price']:,.2f}")
            pct = position["position_size_percent"]
            print(f" Size        : {pct}%")
        else:
            print(" No Paper Position")
        print("-" * 50)
        print(" STATISTICS")
        print("-" * 50)
        _print_stats(stats)
        print("=" * 50)
        print()

        logger.info(
            "Cycle complete — price=%.2f Market=%s Signal=%s "
            "Position=%s Trades=%d Balance=%.2f",
            price, market_state,
            signal.get("signal", "?") if signal else "?",
            "YES" if position else "NO",
            stats["total_trades"],
            balance,
        )

    except Exception as e:
        logger.error("Fatal error: %s", e)
        print()
        print("ERROR")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
