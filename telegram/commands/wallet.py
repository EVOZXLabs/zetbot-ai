from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number


class WalletCommand(BaseCommand):
    meta = CommandMeta(
        name="wallet",
        aliases=["w", "wallet-info"],
        description="Show wallet/balance summary",
        usage="/wallet",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        m = ctx.services.metrics if ctx.services else None
        if m is not None:
            a = m.account()
            bal = a.balance
            eq = a.equity
            net_pnl = a.net_pnl
            realized_pnl = a.realized_pnl
            unrealized_pnl = a.unrealized_pnl
            total_return_pct = a.total_return_pct
            win_rate = a.win_rate
            total_trades = a.total_trades
            positions = m.open_positions()
        else:
            pb = ctx.read_json("paper_balance.json")
            pos_data = ctx.read_json("positions.json")
            if not pb:
                return "No wallet data yet.  Run `/pipeline` first."
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            realized_pnl = pb.get("realized_pnl", 0.0)
            unrealized_pnl = pb.get("unrealized_pnl", 0.0)
            total_return_pct = pb.get("total_return_pct", 0.0)
            win_rate = pb.get("win_rate", 0.0)
            total_trades = pb.get("total_trades", 0)
            pos_list = pos_data.get("positions", []) if pos_data else []
            positions = [p for p in pos_list if p.get("status") == "OPEN"]

        pos_value = sum(
            p.get("position_size_usdt", 0)
            or p.get("cost_basis", 0)
            or (p.get("entry_price", 0) * p.get("quantity", 0))
            for p in positions
        )
        exposure_pct = (pos_value / (eq + pos_value) * 100) if (eq + pos_value) > 0 else 0.0

        return (
            f"\U0001f911 *Wallet*\n"
            f"Cash: `{fmt_compact_number(bal)}`\n"
            f"In Position: `{fmt_compact_number(pos_value)}`\n"
            f"Equity: `{fmt_compact_number(eq)}`\n"
            f"Exposure: `{exposure_pct:.1f}%`\n"
            f"\n"
            f"Net PnL: `{net_pnl:+,.2f}` ({total_return_pct:+.2f}%)\n"
            f"Realized: `{realized_pnl:+,.2f}`  "
            f"Unrealized: `{unrealized_pnl:+,.2f}`\n"
            f"Win Rate: `{win_rate:.1f}%` (`{total_trades}` trades)"
        )
