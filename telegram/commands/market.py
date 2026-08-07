"""/market — market overview, computed live from the active exchange.

Analyzes the top volume candidates in realtime and summarizes market
bias (bullish/bearish split, BTC/ETH trend, strongest pair). Never
reads the stale scanner snapshot.
"""

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import compact_header, build_message, wib_now
from scripts.realtime_market import (
    RealtimeMarketError,
    data_footer,
    get_realtime_service,
)

_DEFAULT_TOP = 16


class MarketCommand(BaseCommand):
    meta = CommandMeta(
        name="market",
        aliases=["mkt"],
        description="Market overview computed live from the exchange",
        usage="/market",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            rt = get_realtime_service(ctx)
            symbols = rt.top_candidates(limit=_DEFAULT_TOP)
            results = rt.analyze_many(symbols)
        except RealtimeMarketError as exc:
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                str(exc),
                f"🕐 {wib_now()}",
            )
        except Exception as exc:
            return build_message(
                compact_header(),
                f"\u26a0\ufe0f *Realtime error*",
                f"Unexpected error: {exc}",
                f"🕐 {wib_now()}",
            )

        if not results:
            return build_message(
                compact_header(),
                "🌍 *Market Overview*",
                f"No pairs could be analyzed on {rt.exchange_name} "
                "right now. Please retry in a moment.",
                f"🕐 {wib_now()}",
            )

        total = len(results)
        bullish = sum(1 for r in results if r.trend_alignment == "BULLISH")
        bearish = sum(1 for r in results if r.trend_alignment == "BEARISH")

        def _trend(sym: str) -> str:
            for r in results:
                if r.symbol.startswith(sym):
                    return r.trend_alignment or "N/A"
            return "N/A"

        strongest = max(results, key=lambda r: r.overall)
        strongest_sym = (
            f"{strongest.symbol} ({strongest.overall:.0f})"
            if strongest.overall > 0
            else "N/A"
        )

        bullish_pct = (bullish / total * 100) if total > 0 else 0
        bearish_pct = (bearish / total * 100) if total > 0 else 0

        blocks = [
            compact_header(),
            f"🌍 *Market Overview* — {rt.exchange_name} "
            f"(analyzed {total} pairs)",
            f"₿ BTC: {_trend('BTC')}  ·  ⟠ ETH: {_trend('ETH')}",
            f"📊 Market Bias\n"
            f"🟢 Bullish: {bullish_pct:.0f}% ({bullish}) · "
            f"🔴 Bearish: {bearish_pct:.0f}% ({bearish})",
            f"🏆 Strongest: {strongest_sym}",
        ]
        if results:
            blocks.append(
                data_footer(max(results, key=lambda r: r.fetched_at)),
            )

        return build_message(*blocks)
