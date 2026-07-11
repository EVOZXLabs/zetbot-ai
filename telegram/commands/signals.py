import json

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price


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
                trend = p.get("trend_alignment", "N/A")
                rsi = p.get("rsi14", 0) or 0
                adx = p.get("adx14", 0) or 0
                atr = p.get("atr_pct", 0) or 0
                ema200 = p.get("ema200", 0) or 0
                volume = p.get("volume_24h", 0) or 0
                price = p.get("price", 0) or 0

                # Build indicator-based reasons
                reasons = []
                if trend == "BULLISH":
                    reasons.append("Bullish trend alignment")
                elif trend == "BEARISH":
                    reasons.append("Bearish trend alignment")
                if rsi > 60:
                    reasons.append(f"RSI momentum ({rsi:.0f})")
                elif rsi < 40:
                    reasons.append(f"RSI oversold ({rsi:.0f})")
                else:
                    reasons.append(f"RSI neutral ({rsi:.0f})")
                if adx > 30:
                    reasons.append(f"Strong trend (ADX {adx:.0f})")
                elif adx > 20:
                    reasons.append(f"Trend developing (ADX {adx:.0f})")
                else:
                    reasons.append(f"Low trend (ADX {adx:.0f})")
                if ema200 > 0 and price > 0:
                    ema_dist = ((price - ema200) / ema200 * 100)
                    if abs(ema_dist) > 1:
                        reasons.append(f"Price vs EMA200: {ema_dist:+.1f}%")
                if atr > 0:
                    reasons.append(f"ATR {atr:.2f}%")

                candidates.append({
                    "symbol": p.get("symbol", "?"),
                    "price": price,
                    "score": score,
                    "trend": trend,
                    "signal": signal,
                    "rsi": rsi,
                    "adx": adx,
                    "atr": atr,
                    "ema200": ema200,
                    "volume": volume,
                    "reasons": "; ".join(reasons) if reasons else "—",
                })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        top = candidates[:10]

        if not top:
            return "No BUY/WATCHLIST signals found in latest scan."

        lines = ["\U0001f4e1 *Top Signals*"]
        lines.append("```")
        lines.append(
            f"{'#':>3s}  {'Pair':>12s}  {'Conf':>5s}  {'Trend':>8s}  "
            f"{'RSI':>5s}  {'ADX':>5s}  Signal"
        )
        lines.append(
            f"{'-'*3}  {'-'*12}  {'-'*5}  {'-'*8}  "
            f"{'-'*5}  {'-'*5}  {'-'*12}"
        )
        for i, c in enumerate(top, 1):
            lines.append(
                f"{i:3d}  {c['symbol']:>12s}  {c['score']:5.1f}  {c['trend']:>8s}  "
                f"{c['rsi']:5.1f}  {c['adx']:5.1f}  {c['signal']}"
            )
        lines.append("```")

        # Detailed breakdown for top 5
        lines.append("")
        for c in top[:5]:
            ema_dist = ((c.get('price', 0) - c['ema200']) / c['ema200'] * 100) if c['ema200'] > 0 and c.get('price', 0) > 0 else 0
            lines.append(
                f"*{c['symbol']}* — `{c['score']:.0f}%` {c['signal']}\n"
                f"Price: `{fmt_price(c['price'])}`  Trend: `{c['trend']}`\n"
                f"EMA200: `{fmt_price(c['ema200'])}`  Distance: `{ema_dist:+.2f}%`\n"
                f"RSI: `{c['rsi']:.1f}`  ADX: `{c['adx']:.1f}`  ATR: `{c['atr']:.2f}%`\n"
                f"Volume: `{fmt_compact_number(c['volume'])}`\n"
                f"Reason: `{c['reasons']}`"
            )

        return "\n\n".join(lines)
