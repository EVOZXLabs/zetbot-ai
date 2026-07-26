from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_price
from telegram.ui import (
    compact_header, wib_now, confidence_bar, build_message,
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
        symbol = args.strip().upper()
        if not symbol:
            return build_message(
                compact_header(),
                "Usage: `/detail <SYMBOL>`\n"
                "Example: `/detail BTC/USDT`",
            )

        scanner = ctx.read_json("scanner_results.json")
        decisions = ctx.read_json("decision_results.json")
        risk = ctx.read_json("risk_results.json")
        trade_plan = ctx.read_json("trade_plan.json")

        # Find scanner data
        pair_data = {}
        for p in scanner.get("pairs", []):
            if p.get("symbol", "").upper() == symbol:
                pair_data = p
                break

        # Find decision
        decision = {}
        for d in decisions.get("decisions", []):
            if d.get("symbol", "").upper() == symbol:
                decision = d
                break

        # Find risk
        risk_entry = {}
        for r in risk.get("results", []):
            if r.get("symbol", "").upper() == symbol:
                risk_entry = r
                break

        # Find trade plan
        plan = {}
        for tp in trade_plan.get("plans", []):
            if tp.get("symbol", "").upper() == symbol:
                plan = tp
                break

        if not pair_data and not decision and not plan:
            return build_message(
                compact_header(),
                f"🔍 *{symbol}*",
                "No data available for this symbol.\n"
                "Run a scan first with /scan.",
            )

        # Extract indicators
        price = pair_data.get("price", 0.0)
        ema200 = pair_data.get("ema200", 0.0) or 0.0
        ema50 = pair_data.get("ema50", 0.0) or 0.0
        ema100 = pair_data.get("ema100", 0.0) or 0.0
        rsi = pair_data.get("rsi14", 0.0) or 0.0
        adx = pair_data.get("adx14", 0.0) or 0.0
        atr = pair_data.get("atr_pct", 0.0) or 0.0
        volume = pair_data.get("volume_24h", 0.0) or 0.0
        trend = pair_data.get("trend_alignment", "N/A")
        signal = pair_data.get("signal", "N/A")
        overall = pair_data.get("overall", 0.0)

        ema200_dist = ((price - ema200) / ema200 * 100) if ema200 > 0 and price > 0 else 0.0

        # From trade plan
        entry = plan.get("entry_price", 0.0)
        sl = plan.get("stop_loss", 0.0)
        tp1 = plan.get("tp1", 0.0)
        risk_reward = plan.get("risk_reward", 0.0)
        risk_pct = plan.get("risk_pct", 0.0)
        reward_pct = plan.get("reward_pct", 0.0)
        conf = plan.get("confidence", decision.get("confidence", 0.0))

        # From decision
        recommendation = decision.get("recommendation", "N/A")

        blocks = [
            compact_header(),
            f"🔍 *{symbol}*",
        ]

        # Price & Trend
        price_block = f"💰 Price\n{fmt_price(price)}"
        if ema200 > 0:
            price_block += f"\n\n📐 EMA200 Distance\n{ema200_dist:+.2f}%"
        blocks.append(price_block)

        # Indicators
        ind_lines = []
        if rsi:
            rsi_label = "Oversold" if rsi < 30 else ("Overbought" if rsi > 70 else "Neutral")
            ind_lines.append(f"RSI: {rsi:.1f} ({rsi_label})")
        if adx:
            adx_label = "Strong" if adx > 25 else "Weak"
            ind_lines.append(f"ADX: {adx:.1f} ({adx_label})")
        if atr:
            ind_lines.append(f"ATR: {atr:.2f}%")
        if ema50:
            ind_lines.append(f"EMA50: {fmt_price(ema50)}")
        if ema100:
            ind_lines.append(f"EMA100: {fmt_price(ema100)}")
        if ind_lines:
            blocks.append(f"📊 *Indicators*\n" + "\n".join(ind_lines))

        # Trend & Signal
        if trend != "N/A" or signal != "N/A":
            blocks.append(
                f"🧠 *Analysis*\n"
                f"Trend: {trend}\n"
                f"Signal: {signal}\n"
                f"Recommendation: {recommendation}"
            )

        # Trade Plan
        if entry > 0:
            plan_lines = [
                f"Entry: {fmt_price(entry)}",
                f"Stop Loss: {fmt_price(sl)}",
                f"Take Profit: {fmt_price(tp1)}",
            ]
            if risk_reward:
                plan_lines.append(f"Risk/Reward: {risk_reward:.2f}")
            if risk_pct:
                plan_lines.append(f"Risk: {risk_pct:.2f}%")
            if reward_pct:
                plan_lines.append(f"Reward: {reward_pct:.2f}%")
            blocks.append(
                f"📋 *Trade Plan*\n" + "\n".join(plan_lines)
            )

        # Confidence
        if conf:
            blocks.append(
                f"⭐ Confidence\n{confidence_bar(conf)}"
            )

        # Volume
        if volume:
            blocks.append(f"📊 24h Volume\n${volume:,.0f}")

        blocks.append(f"🕐 {wib_now()}")

        return build_message(*blocks)
