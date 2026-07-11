import json
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price, fmt_pct


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
            return "Usage: `/pair <SYMBOL>`\nExample: `/pair BTC`"

        try:
            with open("data/scanner_results.json") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return "No scanner data yet. Run `/pipeline` first."

        pairs = data.get("pairs", data.get("results", data.get("sorted", [])))
        if not isinstance(pairs, list):
            return "No scanner data."

        # Find matching pair by symbol
        match = None
        for p in pairs:
            sym = p.get("symbol", "").upper()
            if sym == query or sym == query + "/USDT" or sym.startswith(query + "/"):
                match = p
                break
            # Also match just the base
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
        rel_vol = match.get("relative_volume", 0)
        volatility = match.get("volatility_score", 0)
        momentum = match.get("momentum_score", 0)
        volume_score = match.get("volume_score", 0)
        liquidity = match.get("liquidity_score", 0)
        rank = match.get("rank", "?")

        ema200_dist = ((price - ema200) / ema200 * 100) if ema200 > 0 and price > 0 else 0

        return (
            f"\U0001f50d *{symbol}*\n"
            f"Price: `{fmt_price(price)}`  Rank: `{rank}`\n"
            f"Trend: `{trend}`  Signal: `{signal}`\n"
            f"Confidence: `{overall:.1f}/100`  Change 24h: `{change:+.2f}%`\n"
            f"\n"
            f"*Indicators*\n"
            f"EMA50: `{fmt_price(ema50)}`\n"
            f"EMA100: `{fmt_price(ema100)}`\n"
            f"EMA200: `{fmt_price(ema200)}`  Distance: `{ema200_dist:+.2f}%`\n"
            f"RSI(14): `{rsi:.1f}`\n"
            f"ADX(14): `{adx:.1f}`\n"
            f"ATR: `{atr_pct:.2f}%`\n"
            f"\n"
            f"*Scores*\n"
            f"Momentum: `{momentum:.0f}/100`  Volatility: `{volatility:.0f}/100`\n"
            f"Volume: `{volume_score:.0f}/100`  Liquidity: `{liquidity:.0f}/100`\n"
            f"Overall: `{overall:.1f}/100`\n"
            f"\n"
            f"Volume 24h: `{fmt_compact_number(volume)}`  Rel Vol: `{rel_vol:.2f}x`"
        )
