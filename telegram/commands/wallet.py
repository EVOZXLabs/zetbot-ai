from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_balance, fmt_pnl
from telegram.ui import (
    compact_header, pnl_emoji, exposure_bar, progress_bar,
    detail_block, note, build_message,
)
from scripts.position_status import is_open
from scripts.balance_resolver import resolve_initial_balance


class WalletCommand(BaseCommand):
    """``/wallet`` — quick-glance balance (see ``balance.py`` for the
    detailed breakdown version, ``/balance``).

    Both commands read from the exact same numbers below (``_data()``)
    so they can never drift into disagreeing figures — only how much of
    that data is *shown* differs: /wallet is the short version for a
    quick check, /balance adds the full cash/PnL/exposure breakdown.
    """

    meta = CommandMeta(
        name="wallet",
        aliases=["w", "wallet-info"],
        description="Quick balance check — total, today, all-time",
        usage="/wallet",
        permission="user",
    )

    show_breakdown = False

    def _data(self, ctx):
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
                return "No wallet data yet. Run /pipeline first."
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
            def _unrealized(p: dict) -> float:
                current = p.get("current_price", 0.0)
                total_qty = p.get("quantity", 0.0)
                remaining = p.get("remaining_qty", total_qty)
                cost_basis = p.get("cost_basis", 0.0)
                if cost_basis <= 0 and p.get("entry_price", 0.0) > 0:
                    cost_basis = p["entry_price"] * total_qty
                # Scale cost_basis to remaining portion (partial TP may have
                # reduced remaining_qty while cost_basis still represents
                # the original full-position cost).
                cost_remaining = (
                    cost_basis * (remaining / total_qty)
                    if total_qty > 0
                    else cost_basis
                )
                return current * remaining - cost_remaining

            unrealized = sum(_unrealized(p) for p in positions)
            in_positions_pct = (
                (pos_value / total_balance * 100) if total_balance > 0 else 0.0
            )
            today_pnl = net_pnl  # fallback — same as /status

        return {
            "cash": cash,
            "pos_value": pos_value,
            "realized": realized,
            "unrealized": unrealized,
            "total_balance": total_balance,
            "net_pnl": net_pnl,
            "total_return_pct": total_return_pct,
            "win_rate": win_rate,
            "total_trades": total_trades,
            "in_positions_pct": in_positions_pct,
            "today_pnl": today_pnl,
        }

    def execute(self, ctx, args: str) -> str:
        d = self._data(ctx)
        if isinstance(d, str):
            return d  # "no data yet" message

        quote = getattr(ctx.services.config, "quote_currency", "USDT") if ctx.services else "USDT"
        today_emoji = "🟢" if d["today_pnl"] >= 0 else "🔴"

        blocks = [
            compact_header(),
            f"👛 *Wallet* — {fmt_balance(d['total_balance'], quote)}\n"
            f"Today {today_emoji} {fmt_pnl(d['today_pnl'], quote)} · "
            f"{pnl_emoji(d['net_pnl'])} {fmt_pnl(d['net_pnl'], quote)} "
            f"({d['total_return_pct']:+.2f}%) all-time",
            f"In open trades {exposure_bar(d['in_positions_pct'])}\n"
            f"Win rate {progress_bar(d['win_rate'], 100, 10)} "
            f"{d['win_rate']:.1f}% ({d['total_trades']} trades)",
        ]

        if self.show_breakdown:
            blocks.insert(2, note("Total Balance = cash + current value of open trades"))
            blocks.append(
                detail_block(
                    [
                        f"Cash (idle)      {fmt_balance(d['cash'], quote)}",
                        f"Open trades      {fmt_balance(d['pos_value'], quote)}",
                        f"Closed P&L       {fmt_pnl(d['realized'], quote)}",
                        f"Open P&L         {fmt_pnl(d['unrealized'], quote)}",
                        f"Total Balance    {fmt_balance(d['total_balance'], quote)}",
                        f"In open trades   {d['in_positions_pct']:.1f}%",
                    ],
                    label="Full breakdown",
                )
            )
        else:
            blocks.append("_Type /balance for the full cash & P&L breakdown._")

        return build_message(*blocks)
