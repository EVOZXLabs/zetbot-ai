"""/pair — detailed analysis for a symbol.

Realtime: fetches the live ticker + OHLCV from the ACTIVE exchange
provider and recomputes all indicators at call time. Never reads the
stale scanner snapshot file.
"""

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price
from telegram.ui import compact_header, confidence_bar, build_message, wib_now
from scripts.realtime_market import (
    RealtimeMarketError,
    data_footer,
    get_realtime_service,
)


class PairCommand(BaseCommand):
    meta = CommandMeta(
        name="pair",
        aliases=["symbol"],
        description="Show detailed analysis for a symbol",
        usage="/pair <SYMBOL>",
        permission="user",
        examples=["/pair BTC", "/pair ETH/USDT"],
    )

    def execute(self, ctx, args: str) -> str:
        query = args.strip().upper().replace(" ", "")
        if not query:
            return build_message(
                compact_header(),
                "Usage: /pair <SYMBOL>\n"
                "Example: /pair BTC",
            )

        try:
            rt = get_realtime_service(ctx)
            symbol = rt.resolve_symbol(query)
            a = rt.analyze(symbol)
        except RealtimeMarketError as exc:
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                f"🔍 *{query}*\n{exc}",
                f"🕐 {wib_now()}",
            )
        except Exception as exc:
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                f"🔍 *{query}*\nUnexpected error: {exc}",
                f"🕐 {wib_now()}",
            )

        ema200_dist = (
            ((a.price - a.ema200) / a.ema200 * 100)
            if a.ema200 > 0 and a.price > 0
            else 0.0
        )
        quote = a.symbol.split("/")[1] if "/" in a.symbol else "USDT"

        blocks = [
            compact_header(),
            f"🔍 *{a.symbol}*\n"
            f"💰 Price: {fmt_price(a.price)}\n"
            f"📈 Change: {a.change_24h:+.2f}%\n"
            f"📊 Trend: {a.trend_alignment}  Signal: {a.signal}",
            f"📊 *Indicators*\n"
            f"RSI: {a.rsi14:.1f}  ADX: {a.adx14:.1f}\n"
            f"ATR: {a.atr_pct:.2f}%\n"
            f"EMA50: {fmt_price(a.ema50)}\n"
            f"EMA100: {fmt_price(a.ema100)}\n"
            f"EMA200: {fmt_price(a.ema200)} ({ema200_dist:+.1f}%)",
            f"⭐ Confidence\n{confidence_bar(a.overall)}\n\n"
            f"📊 Volume\n{fmt_compact_number(a.volume_24h, quote)}",
            data_footer(a),
        ]

        return build_message(*blocks)
