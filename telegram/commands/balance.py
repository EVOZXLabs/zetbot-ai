from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import header, SEPARATOR, pnl_emoji, build_message


class BalanceCommand(BaseCommand):
    meta = CommandMeta(
        name="balance",
        aliases=["bal", "equity"],
        description="Account balance, equity and PnL",
        usage="/balance",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        m = ctx.services.metrics if ctx.services else None
        if m is not None:
            a = m.account()
            bal = a.balance
            eq = a.equity
            realized_pnl = a.realized_pnl
            unrealized_pnl = a.unrealized_pnl
            net_pnl = a.net_pnl
            total_return_pct = a.total_return_pct
        else:
            pb = ctx.read_json("paper_balance.json")
            if not pb:
                return "No balance data yet. Run `/pipeline` first."
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            realized_pnl = pb.get("realized_pnl", 0.0)
            unrealized_pnl = pb.get("unrealized_pnl", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            total_return_pct = pb.get("total_return_pct", 0.0)

        return build_message(
            header(),
            f"💰 *BALANCE*\n{SEPARATOR}",
            f"Cash\n${bal:,.2f}\n\nEquity\n${eq:,.2f}",
            f"{SEPARATOR}\n"
            f"📈 Realized\n{pnl_emoji(realized_pnl)} ${realized_pnl:+,.2f}\n\n"
            f"📊 Unrealized\n{pnl_emoji(unrealized_pnl)} ${unrealized_pnl:+,.2f}",
            f"{SEPARATOR}\n"
            f"💰 Net PnL\n{pnl_emoji(net_pnl)} ${net_pnl:+,.2f}\n\n"
            f"📉 Return\n{total_return_pct:+.2f}%",
        )
