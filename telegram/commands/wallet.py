import os

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price


PAUSE_FILE = "data/.paused"


class WalletCommand(BaseCommand):
    meta = CommandMeta(
        name="wallet",
        aliases=["w", "wallet-info"],
        description="Show wallet/balance summary",
        usage="/wallet",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        pb = ctx.read_json("paper_balance.json")
        pos_data = ctx.read_json("positions.json")
        if not pb:
            return "No wallet data yet."

        cash = pb.get("final_balance", 0.0)
        equity = pb.get("final_equity", 0.0)
        realized = pb.get("realized_pnl", 0.0)
        unrealized = pb.get("unrealized_pnl", 0.0)
        net_pnl = pb.get("net_pnl", 0.0)
        initial = pb.get("initial_balance", 10000.0)
        ret_pct = ((equity - initial) / initial * 100) if initial > 0 else 0.0
        total_trades = pb.get("total_trades", 0)
        win_rate = pb.get("win_rate", 0.0)

        pos_list = pos_data.get("positions", []) if pos_data else []
        open_positions = [p for p in pos_list if p.get("status") == "OPEN"]
        pos_value = sum(p.get("position_size_usdt", 0) for p in open_positions)
        exposure_pct = (pos_value / equity * 100) if equity > 0 else 0.0
        used_margin = pos_value
        free_balance = cash

        paused = os.path.exists(PAUSE_FILE)
        buying_power = cash

        return (
            f"\U0001f911 *Wallet*\n"
            f"Cash: `{fmt_compact_number(cash)}`\n"
            f"In Position: `{fmt_compact_number(pos_value)}`\n"
            f"Equity: `{fmt_compact_number(equity)}`\n"
            f"Free Balance: `{fmt_compact_number(free_balance)}`\n"
            f"Exposure: `{exposure_pct:.1f}%`\n"
            f"Buying Power: `{fmt_compact_number(buying_power)}`\n"
            f"\n"
            f"Net PnL: `{net_pnl:+,.2f}` ({ret_pct:+.2f}%)\n"
            f"Realized: `{realized:+,.2f}`  Unrealized: `{unrealized:+,.2f}`\n"
            f"Win Rate: `{win_rate:.1f}%` (`{total_trades}` trades)\n"
            f"Trading: `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`"
        )
