from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    compact_header, pnl_emoji, exposure_bar, progress_bar,
    detail_block, build_message,
)
from scripts.position_status import is_open
from scripts.balance_resolver import resolve_initial_balance


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
            pos_value = a.position_value
            eq = a.equity
            net_pnl = a.net_pnl
            total_return_pct = a.total_return_pct
            win_rate = a.win_rate
            total_trades = a.total_trades
            exposure_pct = a.exposure_pct
        else:
            pb = ctx.read_json("paper_balance.json")
            if not pb:
                return "No wallet data yet. Run `/pipeline` first."
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            initial = resolve_initial_balance(pb, ctx.read_json("paper_state.json"))
            total_return_pct = (
                ((eq - initial) / initial * 100.0) if initial > 0 else 0.0
            )
            win_rate = pb.get("win_rate", 0.0)
            total_trades = pb.get("total_trades", 0)

            # Compute position value and exposure from positions
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", []) if pos_data else []
            positions = [p for p in pos_list if is_open(p.get("status"))]
            pos_value = sum(
                p.get("position_size_usdt", 0)
                or p.get("cost_basis", 0)
                or (p.get("entry_price", 0) * p.get("quantity", 0))
                for p in positions
            )
            exposure_pct = (pos_value / eq * 100) if eq > 0 else 0.0

        # Same headline numbers as /balance (equity, net PnL, return) so the
        # two commands never appear to disagree — /wallet adds exposure and
        # win-rate on top, it doesn't recompute PnL a different way.
        return build_message(
            compact_header(),
            f"👛 *Wallet* — ${eq:,.2f}\n"
            f"{pnl_emoji(net_pnl)} {net_pnl:+,.2f} ({total_return_pct:+.2f}%) all-time",
            f"Exposure {exposure_bar(exposure_pct)}\n"
            f"Win rate {progress_bar(win_rate, 100, 10)} {win_rate:.1f}% ({total_trades} trades)",
            detail_block([
                f"Cash        ${bal:,.2f}",
                f"In Position ${pos_value:,.2f}",
            ]),
        )
