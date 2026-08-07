"""/detail — advanced indicator analysis for a symbol.

Realtime: fetches the live ticker + OHLCV from the ACTIVE exchange
provider and recomputes all indicators (EMA50/100/200, RSI, ADX, ATR,
trend, signal, recommendation) at call time. Never reads the stale
scanner/decision/risk/trade-plan snapshot files.
"""

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price
from telegram.ui import (
    compact_header, wib_now, confidence_bar, build_message,
)
from scripts.realtime_market import (
    RealtimeMarketError,
    data_footer,
    get_realtime_service,
)


class DetailCommand(BaseCommand):
    meta = CommandMeta(
        name="detail",
        aliases=["d"],
        description="Advanced indicator analysis for a symbol",
        usage="/detail <SYMBOL>",
        permission="user",
        examples=["/detail BTC/USDT", "/detail ETH/USDT"],
    )

    def execute(self, ctx, args: str) -> str:
        symbol_query = args.strip().upper()
        if not symbol_query:
            return build_message(
                compact_header(),
                "Usage: /detail <SYMBOL>\n"
                "Example: /detail BTC/USDT",
            )

        try:
            rt = get_realtime_service(ctx)
            symbol = rt.resolve_symbol(symbol_query)
            a = rt.analyze(symbol)
        except RealtimeMarketError as exc:
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                f"🔍 *{symbol_query}*\n{exc}",
                f"🕐 {wib_now()}",
            )
        except Exception as exc:  # never let the exchange break Telegram
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                f"🔍 *{symbol_query}*\nUnexpected error: {exc}",
                f"🕐 {wib_now()}",
            )

        price = a.price
        ema200 = a.ema200
        ema200_dist = (
            ((price - ema200) / ema200 * 100) if ema200 > 0 and price > 0 else 0.0
        )

        blocks = [
            compact_header(),
            f"🔍 *{a.symbol}*\n"
            f"💰 Price: {fmt_price(price)}\n"
            f"📈 Change: {a.change_24h:+.2f}%",
        ]

        # Indicators
        ind_lines = []
        rsi_label = (
            "Oversold" if a.rsi14 < 30
            else ("Overbought" if a.rsi14 > 70 else "Neutral")
        )
        adx_label = "Strong" if a.adx14 > 25 else "Weak"
        ind_lines.append(f"RSI: {a.rsi14:.1f} ({rsi_label})")
        ind_lines.append(f"ADX: {a.adx14:.1f} ({adx_label})")
        ind_lines.append(f"ATR: {a.atr_pct:.2f}%")
        ind_lines.append(f"EMA50: {fmt_price(a.ema50)}")
        ind_lines.append(f"EMA100: {fmt_price(a.ema100)}")
        ind_lines.append(f"EMA200: {fmt_price(a.ema200)} ({ema200_dist:+.1f}%)")
        blocks.append(f"📊 *Indicators*\n" + "\n".join(ind_lines))

        # Trend, signal, recommendation (recomputed right now)
        blocks.append(
            f"🧠 *Analysis*\n"
            f"Trend: {a.trend_alignment}\n"
            f"Signal: {a.signal}\n"
            f"Recommendation: {a.recommendation}"
        )

        # Confidence
        if a.overall > 0:
            blocks.append(f"⭐ Confidence\n{confidence_bar(a.overall)}")

        # Volume
        quote = a.symbol.split("/")[1] if "/" in a.symbol else "USDT"
        blocks.append(f"📊 24h Volume\n{fmt_compact_number(a.volume_24h, quote)}")

        # Transparency: source timestamp + age
        blocks.append(data_footer(a))

        return build_message(*blocks)
