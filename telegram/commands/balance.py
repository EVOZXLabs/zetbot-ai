from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import compact_header, pnl_emoji, detail_block, build_message
from scripts.balance_resolver import resolve_initial_balance


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
            initial = resolve_initial_balance(pb, ctx.read_json("paper_state.json"))
            total_return_pct = (
                ((eq - initial) / initial * 100.0) if initial > 0 else 0.0
            )

        # One number per idea: Equity is the headline, Net PnL explains the
        # change, Return gives the same change as a percentage — all three
        # must move together, so we never show a return figure that
        # contradicts a positive/negative Net PnL.
        return build_message(
            compact_header(),
            f"💰 *Balance* — ${eq:,.2f}\n"
            f"{pnl_emoji(net_pnl)} {net_pnl:+,.2f} ({total_return_pct:+.2f}%) all-time",
            detail_block([
                f"Cash        ${bal:,.2f}",
                f"Realized    {realized_pnl:+,.2f}",
                f"Unrealized  {unrealized_pnl:+,.2f}",
            ]),
        )
