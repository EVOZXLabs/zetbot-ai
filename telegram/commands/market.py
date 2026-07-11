import json
import math
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta


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
        neutral = total - bullish - bearish

        bullish_pct = (bullish / total * 100) if total > 0 else 0.0
        bearish_pct = (bearish / total * 100) if total > 0 else 0.0
        neutral_pct = (neutral / total * 100) if total > 0 else 0.0

        # Highest volume
        sorted_by_vol = sorted(
            [p for p in pairs if p.get("volume_24h", 0) > 0],
            key=lambda p: p["volume_24h"], reverse=True
        )
        highest_vol = sorted_by_vol[0]["symbol"] if sorted_by_vol else "N/A"

        # Strongest/weakest signal by overall score
        scored = [p for p in pairs if p.get("overall", 0) > 0]
        scored.sort(key=lambda p: p["overall"], reverse=True)
        strongest = scored[0]["symbol"] if scored else "N/A"
        weakest = scored[-1]["symbol"] if scored else "N/A"

        # BTC trend
        btc_pair = next((p for p in pairs if p.get("symbol", "").startswith("BTC")), None)
        eth_pair = next((p for p in pairs if p.get("symbol", "").startswith("ETH")), None)

        def _trend(p: Any) -> str:
            if p is None:
                return "N/A"
            return p.get("trend_alignment", "N/A")

        def _signal(p: Any) -> str:
            if p is None:
                return "N/A"
            return p.get("signal", "N/A")

        lines = [
            "\U0001f30d *Market Overview*",
            f"BTC: `{_trend(btc_pair)}`  Signal: `{_signal(btc_pair)}`",
            f"ETH: `{_trend(eth_pair)}`  Signal: `{_signal(eth_pair)}`",
            f"",
            f"Market Bias",
            f"Bullish: `{bullish_pct:.0f}%` ({bullish})",
            f"Bearish: `{bearish_pct:.0f}%` ({bearish})",
            f"Neutral: `{neutral_pct:.0f}%` ({neutral})",
            f"",
            f"Highest Volume: `{highest_vol}`",
            f"Strongest Signal: `{strongest}`",
            f"Weakest Signal: `{weakest}`",
        ]

        return "\n".join(lines)
