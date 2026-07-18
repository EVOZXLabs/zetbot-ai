import json
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import header, SEPARATOR, progress_bar, build_message


class MarketCommand(BaseCommand):
    meta = CommandMeta(
        name="market",
        aliases=["mkt"],
        description="Market overview from latest scan",
        usage="/market",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            with open("data/scanner_results.json") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return "No scanner data yet. Run `/pipeline` first."

        pairs = data.get("pairs", data.get("results", data.get("sorted", [])))
        if not isinstance(pairs, list) or not pairs:
            return "No pairs found."

        total = len(pairs)
        bullish = sum(1 for p in pairs if p.get("trend_alignment", "").upper() == "BULLISH")
        bearish = sum(1 for p in pairs if p.get("trend_alignment", "").upper() == "BEARISH")

        btc_pair = next((p for p in pairs if p.get("symbol", "").startswith("BTC")), None)
        eth_pair = next((p for p in pairs if p.get("symbol", "").startswith("ETH")), None)

        def _trend(p: Any) -> str:
            return p.get("trend_alignment", "N/A") if p else "N/A"

        scored = [p for p in pairs if p.get("overall", 0) > 0]
        scored.sort(key=lambda p: p["overall"], reverse=True)
        strongest = scored[0]["symbol"] if scored else "N/A"

        bullish_pct = (bullish / total * 100) if total > 0 else 0
        bearish_pct = (bearish / total * 100) if total > 0 else 0

        return build_message(
            header(),
            f"🌍 *MARKET OVERVIEW*\n{SEPARATOR}",
            f"₿ BTC: {_trend(btc_pair)}\n"
            f"⟠ ETH: {_trend(eth_pair)}",
            f"{SEPARATOR}\n"
            f"📊 Market Bias\n"
            f"🟢 Bullish: {bullish_pct:.0f}% ({bullish})\n"
            f"🔴 Bearish: {bearish_pct:.0f}% ({bearish})",
            f"{SEPARATOR}\n"
            f"🏆 Strongest: {strongest}",
        )
