"""/signals — top trading signals, recomputed live.

Scans the top volume candidates on the ACTIVE exchange in realtime
(ticker + OHLCV + indicators at call time), ranks them, and shows the
strongest BUY/WATCHLIST signals. Never reads the stale scanner snapshot.
"""

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_price
from telegram.ui import compact_header, confidence_bar, build_message, wib_now
from scripts.realtime_market import (
    RealtimeMarketError,
    data_footer,
    fmt_age,
    fmt_wib,
    get_realtime_service,
)

_SIGNAL_POOL = ("STRONG BUY", "BUY", "WATCHLIST")
_TOP_DISPLAY = 8


class SignalsCommand(BaseCommand):
    meta = CommandMeta(
        name="signals",
        aliases=["signal"],
        description="Show live trading signals computed from the exchange now",
        usage="/signals",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            rt = get_realtime_service(ctx)
            symbols = rt.top_candidates(chat_id=ctx.chat_id)
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

        candidates = [
            r for r in results if r.signal in _SIGNAL_POOL
        ]
        candidates.sort(key=lambda r: r.overall, reverse=True)
        top = candidates[:_TOP_DISPLAY]

        footer_blocks = []
        if results:
            newest = max(r.fetched_at for r in results)
            footer_blocks.append(
                f"Data Time: {fmt_wib(newest)}\n"
                f"Data Age: {fmt_age(newest)}"
            )

        if not top:
            blocks = [
                compact_header(),
                "📡 *Top Signals*",
                f"No BUY/WATCHLIST signals found in the realtime scan of "
                f"{len(results)} pairs on {rt.exchange_name}.",
            ]
            blocks.extend(footer_blocks)
            return build_message(*blocks)

        blocks = [
            compact_header(),
            f"📡 *Top Signals* — {rt.exchange_name} "
            f"(scanned {len(results)} pairs)",
        ]

        for i, r in enumerate(top, 1):
            emoji = "🟢" if "BUY" in r.signal else "🟡"
            blocks.append(
                f"{emoji} *{r.symbol}* — {r.signal}\n"
                f"💰 {fmt_price(r.price)}  ·  {r.change_24h:+.2f}%\n"
                f"{confidence_bar(r.overall)}\n"
                f"RSI: {r.rsi14:.0f}  ADX: {r.adx14:.0f}  "
                f"Trend: {r.trend_alignment}"
            )

        if results:
            blocks.append(data_footer(max(results, key=lambda r: r.fetched_at)))

        return build_message(*blocks)
