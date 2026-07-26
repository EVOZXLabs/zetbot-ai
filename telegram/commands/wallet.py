from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    compact_header, pnl_emoji, exposure_bar, progress_bar,
    detail_block, note, build_message,
)
from scripts.position_status import is_open
from scripts.balance_resolver import resolve_initial_balance


class WalletCommand(BaseCommand):
    """Canonical account view — also used by ``/balance`` (see balance.py).

    /balance and /wallet used to be two separate templates for the same
    overlapping numbers (both showed equity, PnL, cash...) which could
    drift out of sync and confuse users about which one to trust. Now
    there is exactly one implementation; /balance is just a shorter
    alias name that renders the same message.
    """

    meta = CommandMeta(
        name="wallet",
        aliases=["w", "wallet-info"],
        description="Your account balance, PnL and exposure",
        usage="/wallet",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        m = ctx.services.metrics if ctx.services else None

        if m is not None:
            a = m.account()
            cash = a.balance
            pos_value = a.position_value
            realized = a.realized_pnl
            unrealized = a.unrealized_pnl
            total_balance = a.equity
            net_pnl = a.net_pnl
            total_return_pct = a.total_return_pct
            win_rate = a.win_rate
            total_trades = a.total_trades
            in_positions_pct = a.exposure_pct
            today_pnl = m.today_summary().get("pnl", 0.0)
        else:
            pb = ctx.read_json("paper_balance.json")
            if not pb:
                return "No wallet data yet. Run `/pipeline` first."
            cash = pb.get("final_balance", 0.0)
            total_balance = pb.get("final_equity", 0.0)
            realized = pb.get("realized_pnl", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            initial = resolve_initial_balance(pb, ctx.read_json("paper_state.json"))
            total_return_pct = (
                ((total_balance - initial) / initial * 100.0) if initial > 0 else 0.0
            )
            win_rate = pb.get("win_rate", 0.0)
            total_trades = pb.get("total_trades", 0)

            # Position value derived the same way as MetricsManager
            # (equity - cash) so this branch can never disagree with the
            # canonical snapshot used elsewhere.
            pos_value = total_balance - cash
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", []) if pos_data else []
            positions = [p for p in pos_list if is_open(p.get("status"))]
            unrealized = sum(
                p.get("current_price", 0.0) * p.get("remaining_qty", p.get("quantity", 0.0))
                - p.get("cost_basis", 0.0)
                for p in positions
            )
            in_positions_pct = (
                (pos_value / total_balance * 100) if total_balance > 0 else 0.0
            )
            today_pnl = net_pnl  # fallback — same as /status

        today_emoji = "🟢" if today_pnl >= 0 else "🔴"

        return build_message(
            compact_header(),
            f"👛 *Wallet* — ${total_balance:,.2f}\n"
            f"Today {today_emoji} ${today_pnl:+,.2f} · "
            f"{pnl_emoji(net_pnl)} {net_pnl:+,.2f} ({total_return_pct:+.2f}%) all-time",
            note("Total Balance = cash + current value of open trades"),
            f"In open trades {exposure_bar(in_positions_pct)}\n"
            f"Win rate {progress_bar(win_rate, 100, 10)} {win_rate:.1f}% ({total_trades} trades)",
            detail_block(
                [
                    f"Cash (idle)      ${cash:,.2f}",
                    f"Open trades      ${pos_value:,.2f}",
                    f"Closed P&L       {realized:+,.2f}",
                    f"Open P&L         {unrealized:+,.2f}",
                    f"Total Balance    ${total_balance:,.2f}",
                    f"In open trades   {in_positions_pct:.1f}%",
                ],
                label="Full breakdown",
            ),
        )
