from telegram.base_command import BaseCommand, CommandMeta


class WalletCommand(BaseCommand):
    meta = CommandMeta(
        name="wallet",
        aliases=["w", "wallet-info"],
        description="Show wallet/balance summary",
        usage="/wallet",
        permission="user",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        pb = ctx.read_json("paper_balance.json")
        if not pb:
            return "No wallet data yet."

        return (
            f"\U0001f911 *Wallet*\n"
            f"Balance: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Equity: `${pb.get('final_equity', 0):,.2f}`\n"
            f"Net PnL: `${pb.get('net_pnl', 0):+,.2f}`\n"
            f"Return: `{pb.get('total_return_pct', 0):+.2f}%`"
        )
