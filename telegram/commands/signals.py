import json

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price
from telegram.ui import compact_header, confidence_bar, build_message


class SignalsCommand(BaseCommand):
    meta = CommandMeta(
        name="signals",
        aliases=["signal"],
        description="Show latest trading signals from scanner results",
        usage="/signals",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            with open("data/scanner_results.json") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return "No scanner data yet. Run `/pipeline` first."

        pairs = data.get("results", data.get("pairs", data.get("sorted", [])))
        if not isinstance(pairs, list) or not pairs:
            return "No pairs found in scanner results."

        candidates = []
        for p in pairs:
            if not isinstance(p, dict):
                continue
            signal = (p.get("signal") or p.get("classification") or "").upper()
            score = p.get("overall", 0) or 0
            if signal in ("STRONG BUY", "BUY", "WATCHLIST"):
                candidates.append({
                    "symbol": p.get("symbol", "?"),
                    "price": p.get("price", 0) or 0,
                    "score": score,
                    "trend": p.get("trend_alignment", "N/A"),
                    "signal": signal,
                    "rsi": p.get("rsi14", 0) or 0,
                    "adx": p.get("adx14", 0) or 0,
                    "atr": p.get("atr_pct", 0) or 0,
                    "ema200": p.get("ema200", 0) or 0,
                    "volume": p.get("volume_24h", 0) or 0,
                })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        top = candidates[:8]

        if not top:
            return "No BUY/WATCHLIST signals found in latest scan."

        blocks = [compact_header(), "📡 *Top Signals*"]

        for i, c in enumerate(top, 1):
            emoji = "🟢" if "BUY" in c["signal"] else "🟡"
            block = (
                f"{emoji} *{c['symbol']}* — {c['signal']}\n"
                f"{confidence_bar(c['score'])}\n"
                f"RSI: {c['rsi']:.0f}  ADX: {c['adx']:.0f}  Trend: {c['trend']}"
            )
            blocks.append(block)

        return build_message(*blocks)
