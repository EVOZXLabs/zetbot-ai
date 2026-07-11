from telegram.base_command import BaseCommand, CommandMeta


class BalanceCommand(BaseCommand):
    meta = CommandMeta(
        name="balance",
        aliases=["bal", "equity"],
        description="Account balance, equity and PnL",
        usage="/balance",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        if ctx.services is not None:
            m = ctx.services.metrics
            bal = m.balance()
            eq = m.equity()
            realized_pnl = m.realized_pnl()
            unrealized_pnl = m.unrealized_pnl()
            net_pnl = m.net_pnl()
            total_return_pct = m.total_return_pct()
        else:
            pb = ctx.read_json("paper_balance.json")
            if not pb:
                return "No balance data yet.  Run `/pipeline` first."
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            realized_pnl = pb.get("realized_pnl", 0.0)
            unrealized_pnl = pb.get("unrealized_pnl", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            total_return_pct = pb.get("total_return_pct", 0.0)

        return (
            f"\U0001f4b0 *Balance*\n"
            f"Cash: `${bal:,.2f}`\n"
            f"Equity: `${eq:,.2f}`\n"
            f"Realized PnL: `${realized_pnl:+,.2f}`\n"
            f"Unrealized PnL: `${unrealized_pnl:+,.2f}`\n"
            f"Net PnL: `${net_pnl:+,.2f}`\n"
            f"Return: `{total_return_pct:+.2f}%`"
        )
