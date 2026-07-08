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
        pb = ctx.read_json("paper_balance.json")
        if not pb:
            return "No balance data yet.  Run `/pipeline` first."

        return (
            f"\U0001f4b0 *Balance*\n"
            f"Cash: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Equity: `${pb.get('final_equity', 0):,.2f}`\n"
            f"Realized PnL: `${pb.get('realized_pnl', 0):+,.2f}`\n"
            f"Unrealized PnL: `${pb.get('unrealized_pnl', 0):+,.2f}`\n"
            f"Net PnL: `${pb.get('net_pnl', 0):+,.2f}`\n"
            f"Return: `{pb.get('total_return_pct', 0):+.2f}%`"
        )
