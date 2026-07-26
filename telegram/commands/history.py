from datetime import datetime, timezone

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_price, fmt_holding, time_ago
from telegram.ui import compact_header, pnl_emoji, build_message


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

        orders_data = ctx.read_json("paper_orders.json")
        state_data = ctx.read_json("state.json")

        closed_orders = []
        if orders_data:
            for o in orders_data.get("orders", []):
                if o.get("status") == "CLOSED":
                    closed_orders.append(o)
        if not closed_orders and state_data:
            trades = state_data.get("paper", {}).get("trades", [])
            for t in trades:
                closed_orders.append({
                    "symbol": t.get("symbol", "?"),
                    "side": "BUY",
                    "net_pnl": t.get("net_pnl", 0),
                    "net_pnl_pct": t.get("pnl_pct", 0),
                    "entry_price": t.get("entry_price", 0),
                    "exit_price": t.get("exit_price", 0),
                    "closed_at": t.get("exit_time", ""),
                    "filled_at": t.get("entry_time", ""),
                    "exit_reason": t.get("exit_reason", ""),
                    "total_cost": t.get("entry_price", 0) * t.get("quantity", 0),
                })

        if not closed_orders:
            return build_message(compact_header(), "No completed trades yet.")

        closed_orders.sort(key=lambda o: o.get("closed_at", "") or "", reverse=True)
        shown = closed_orders[:limit]

        blocks = [compact_header(), f"📋 *Trade History* (last {len(shown)})"]

        for o in shown:
            symbol = o.get("symbol", "?")
            entry = o.get("entry_price", 0)
            exit_p = o.get("exit_price", 0)
            pnl = o.get("net_pnl", 0)
            reason = o.get("exit_reason", "?")
            closed_at = o.get("closed_at", "")
            filled = o.get("filled_at", "")

            hold = ""
            if filled and closed_at:
                try:
                    fdt = datetime.fromisoformat(filled.replace("Z", "+00:00"))
                    cdt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                    hold = fmt_holding((cdt - fdt).total_seconds())
                except (ValueError, TypeError):
                    pass

            block = (
                f"{pnl_emoji(pnl)} *{symbol}*  ${pnl:+,.2f}\n"
                f"💰 {fmt_price(entry)} → 🚪 {fmt_price(exit_p)}"
                f"{f'  ·  🕒 {hold}' if hold else ''}\n"
                f"📋 {reason}"
            )
            blocks.append(block)

        return build_message(*blocks)
