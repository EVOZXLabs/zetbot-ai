import json

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price
from telegram.ui import compact_header, confidence_bar, build_message


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
                "Usage: `/pair <SYMBOL>`\n"
                "Example: `/pair BTC`",
            )

        try:
            with open("data/scanner_results.json") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return "No scanner data yet. Run `/pipeline` first."

        pairs = data.get("pairs", data.get("results", data.get("sorted", [])))
        if not isinstance(pairs, list):
            return "No scanner data."

        match = None
        for p in pairs:
            sym = p.get("symbol", "").upper()
            if sym == query or sym == query + "/USDT" or sym.startswith(query + "/"):
                match = p
                break
            base = p.get("base", "").upper()
            if base == query:
                match = p
                break

        if match is None:
            return f"Pair `{query}` not found in latest scan."

        symbol = match.get("symbol", "?")
        price = match.get("price", 0)
        trend = match.get("trend_alignment", "N/A")
        signal = match.get("signal", "N/A")
        overall = match.get("overall", 0)
        rsi = match.get("rsi14", 0)
        adx = match.get("adx14", 0)
        atr_pct = match.get("atr_pct", 0)
        ema200 = match.get("ema200", 0)
        ema50 = match.get("ema50", 0)
        ema100 = match.get("ema100", 0)
        volume = match.get("volume_24h", 0)
        change = match.get("change_24h", 0)

        ema200_dist = ((price - ema200) / ema200 * 100) if ema200 > 0 and price > 0 else 0

        blocks = [
            compact_header(),
            f"🔍 *{symbol}*\n"
            f"💰 Price: {fmt_price(price)}\n"
            f"📈 Change: {change:+.2f}%\n"
            f"📊 Trend: {trend}  Signal: {signal}",
            f"📊 *Indicators*\n"
            f"RSI: {rsi:.1f}  ADX: {adx:.1f}\n"
            f"ATR: {atr_pct:.2f}%\n"
            f"EMA50: {fmt_price(ema50)}\n"
            f"EMA100: {fmt_price(ema100)}\n"
            f"EMA200: {fmt_price(ema200)} ({ema200_dist:+.1f}%)",
            f"⭐ Confidence\n{confidence_bar(overall)}\n\n"
            f"📊 Volume\n{fmt_compact_number(volume)}",
        ]

        return build_message(*blocks)
