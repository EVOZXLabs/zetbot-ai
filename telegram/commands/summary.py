from datetime import datetime, timezone

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_holding


def _today_filter() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SummaryCommand(BaseCommand):
    meta = CommandMeta(
        name="summary",
        aliases=["overview", "report"],
        description="Today's trading statistics",
        usage="/summary",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        today_str = _today_filter()

        orders_data = ctx.read_json("paper_orders.json")
        all_orders = orders_data.get("orders", []) if orders_data else []

        # Only count BUY orders closed today as today's trades
        today_orders = []
        for o in all_orders:
            if o.get("side") != "BUY":
                continue
            closed = o.get("closed_at", "") or ""
            cnt = o.get("created_at", "") or ""
            if today_str in closed or today_str in cnt:
                today_orders.append(o)

        closed_today = [o for o in today_orders if o.get("status") == "CLOSED"]
        wins = [o for o in closed_today if o.get("net_pnl", 0) >= 0]
        losses = [o for o in closed_today if o.get("net_pnl", 0) < 0]
        total = len(closed_today)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total * 100) if total > 0 else 0.0

        today_pnl = sum(o.get("net_pnl", 0) for o in closed_today)
        gross_profit = sum(o.get("net_pnl", 0) for o in wins)
        gross_loss = abs(sum(o.get("net_pnl", 0) for o in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

        avg_win = (gross_profit / win_count) if win_count > 0 else 0.0
        avg_loss = (gross_loss / loss_count) if loss_count > 0 else 0.0

        best = max(closed_today, key=lambda o: o.get("net_pnl", 0)) if closed_today else None
        worst = min(closed_today, key=lambda o: o.get("net_pnl", 0)) if closed_today else None

        # Cost basis for ROI
        total_cost = sum(o.get("total_cost", 0) for o in closed_today)
        today_roi = (today_pnl / total_cost * 100) if total_cost > 0 else 0.0

        # Average holding time for today's closed trades
        holding_times = []
        for o in closed_today:
            if o.get("filled_at") and o.get("closed_at"):
                try:
                    filled = datetime.fromisoformat(o["filled_at"].replace("Z", "+00:00"))
                    closed = datetime.fromisoformat(o["closed_at"].replace("Z", "+00:00"))
                    holding_times.append((closed - filled).total_seconds())
                except (ValueError, TypeError):
                    pass
        avg_hold_sec = (sum(holding_times) / len(holding_times)) if holding_times else 0.0
        avg_hold_str = fmt_holding(avg_hold_sec)

        lines = [
            f"\U0001f4ca *Today's Summary*",
            f"Date: `{today_str}`",
            f"",
            f"Today's Trades: `{total}`",
            f"Wins: `{win_count}`  Losses: `{loss_count}`",
            f"Win Rate: `{win_rate:.1f}%`",
            f"Today's PnL: `{fmt_compact_number(today_pnl)}`",
            f"Today's ROI: `{today_roi:+.2f}%`",
        ]
        if win_count > 0:
            lines.append(f"Average Win: `{fmt_compact_number(avg_win)}`")
        if loss_count > 0:
            lines.append(f"Average Loss: `{fmt_compact_number(avg_loss)}`")
        if profit_factor > 0:
            lines.append(f"Profit Factor: `{profit_factor:.2f}`")
        if avg_hold_sec > 0:
            lines.append(f"Average Holding Time: `{avg_hold_str}`")
        if best:
            lines.append(f"Best Trade Today: `{best.get('symbol', '?')}`  `{fmt_compact_number(best.get('net_pnl', 0))}`")
        if worst:
            lines.append(f"Worst Trade Today: `{worst.get('symbol', '?')}`  `{fmt_compact_number(worst.get('net_pnl', 0))}`")

        return "\n".join(lines)
