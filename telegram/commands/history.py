from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import (
    fmt_price, fmt_holding, fmt_pnl, order_hold_seconds,
)
from telegram.ui import compact_header, pnl_emoji, build_message
from scripts.metrics_manager import MetricsManager


class HistoryCommand(BaseCommand):
    meta = CommandMeta(
        name="history",
        aliases=["trades", "closed"],
        description="Show last completed trades",
        usage="/history [count=10]",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            limit = max(1, min(50, int(args.strip())))
        except (ValueError, TypeError):
            limit = 10

        # Single source of truth for closed trades = MetricsManager
        # (paper_trade_history.csv).  This guarantees /history shows the
        # exact same trades as /summary and the accounting layer — no more
        # divergent paper_orders.json / legacy state.json reads.
        if ctx.services is not None:
            trades = ctx.services.metrics.trade_history()
        else:
            trades = MetricsManager("data").trade_history()

        if not trades:
            return build_message(compact_header(), "No completed trades yet.")

        shown = trades[:limit]

        blocks = [compact_header(), f"📋 *Trade History* (last {len(shown)})"]

        for o in shown:
            symbol = o.get("symbol", "?")
            entry = o.get("entry_price", 0)
            exit_p = o.get("exit_price", 0)
            pnl = o.get("net_pnl", 0)
            reason = o.get("reason", "?")
            closed_at = o.get("exit_time", "") or o.get("closed_at", "")

            hold = ""
            hold_sec = order_hold_seconds(o, {})
            if hold_sec is None:
                et = o.get("entry_time", "")
                xt = o.get("exit_time", "")
                if et and xt:
                    try:
                        fmt = "%Y-%m-%dT%H:%M:%S.%f"
                        e = __import__("datetime").datetime.strptime(
                            et.split("+")[0].split("Z")[0], fmt)
                        x = __import__("datetime").datetime.strptime(
                            xt.split("+")[0].split("Z")[0], fmt)
                        hold_sec = (x - e).total_seconds()
                    except (ValueError, IndexError):
                        hold_sec = None
            if hold_sec is not None:
                hold = fmt_holding(hold_sec)

            quote = symbol.split("/")[1] if "/" in symbol else "USDT"
            block = (
                f"{pnl_emoji(pnl)} *{symbol}*  {fmt_pnl(pnl, quote)}\n"
                f"💰 {fmt_price(entry)} → 🚪 {fmt_price(exit_p)}"
                f"{f'  ·  🕒 {hold}' if hold else ''}\n"
                f"📋 {reason}"
            )
            blocks.append(block)

        return build_message(*blocks)
