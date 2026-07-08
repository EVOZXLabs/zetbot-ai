from typing import Any, Optional

from telegram.base_command import BaseCommand, CommandMeta


class SummaryCommand(BaseCommand):
    meta = CommandMeta(
        name="summary",
        aliases=["overview", "report"],
        description="Today's trading statistics",
        usage="/summary",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        pb = ctx.read_json("paper_balance.json")
        orders_data = ctx.read_json("paper_orders.json")

        total_trades = pb.get("total_trades", 0)
        wins = pb.get("winning_trades", 0)
        losses = pb.get("losing_trades", 0)
        win_rate = pb.get("win_rate", 0.0)
        realized_pnl = pb.get("realized_pnl", 0.0)
        unrealized_pnl = pb.get("unrealized_pnl", 0.0)
        net_pnl = pb.get("net_pnl", 0.0)

        closed_orders = [
            o for o in orders_data.get("orders", [])
            if o.get("status") == "CLOSED"
        ]

        best: Optional[dict[str, Any]] = None
        worst: Optional[dict[str, Any]] = None
        if closed_orders:
            best = max(closed_orders, key=lambda o: o.get("net_pnl", 0))
            worst = min(closed_orders, key=lambda o: o.get("net_pnl", 0))

        lines = [
            f"\U0001f4ca *Trading Summary*",
            f"Today's Trades: `{total_trades}`",
            f"Wins: `{wins}`  "
            f"Losses: `{losses}`",
            f"Win Rate: `{win_rate:.1f}%`",
            f"Realized PnL: `${realized_pnl:+,.2f}`",
            f"Unrealized PnL: `${unrealized_pnl:+,.2f}`",
            f"Net PnL: `${net_pnl:+,.2f}`",
        ]

        if total_trades > 0:
            lines.insert(4, f"Profit Factor: `{pb.get('profit_factor', 0):.2f}`")
            lines.insert(5, f"Gross Profit: `${pb.get('gross_profit', 0):,.2f}`")
            lines.insert(6, f"Gross Loss: `${pb.get('gross_loss', 0):,.2f}`")

        if best:
            lines.append(
                f"Best Trade: `{best['symbol']}`  "
                f"`${best['net_pnl']:+,.2f}`"
            )
        if worst:
            lines.append(
                f"Worst Trade: `{worst['symbol']}`  "
                f"`${worst['net_pnl']:+,.2f}`"
            )

        return "\n".join(lines)
