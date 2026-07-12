import csv
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

        # Read completed trades from paper trade history
        # Source of truth: data/paper_trade_history.csv
        today_orders = []

        try:
            with open("data/paper_trade_history.csv", newline="") as f:
                reader = csv.DictReader(f)

                for row in reader:
                    closed = row.get("closed_at", "") or ""

                    if today_str in closed:
                        row["net_pnl"] = float(row.get("net_pnl", 0) or 0)
                        row["net_pnl_pct"] = float(row.get("net_pnl_pct", 0) or 0)
                        today_orders.append(row)

        except FileNotFoundError:
            today_orders = []

        closed_today = today_orders

        wins = [
            o for o in closed_today
            if o.get("net_pnl", 0) >= 0
        ]

        losses = [
            o for o in closed_today
            if o.get("net_pnl", 0) < 0
        ]

        total = len(closed_today)
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = (
            (win_count / total * 100)
            if total > 0
            else 0.0
        )

        today_pnl = sum(
            o.get("net_pnl", 0)
            for o in closed_today
        )

        gross_profit = sum(
            o.get("net_pnl", 0)
            for o in wins
        )

        gross_loss = abs(sum(
            o.get("net_pnl", 0)
            for o in losses
        ))

        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else gross_profit
        )

        avg_win = (
            gross_profit / win_count
            if win_count > 0
            else 0.0
        )

        avg_loss = (
            gross_loss / loss_count
            if loss_count > 0
            else 0.0
        )

        best = (
            max(
                closed_today,
                key=lambda o: o.get("net_pnl", 0)
            )
            if closed_today
            else None
        )

        worst = (
            min(
                closed_today,
                key=lambda o: o.get("net_pnl", 0)
            )
            if closed_today
            else None
        )

        total_cost = sum(
            float(o.get("quantity", 0))
            * float(o.get("entry_price", 0))
            for o in closed_today
        )

        today_roi = (
            today_pnl / total_cost * 100
            if total_cost > 0
            else 0.0
        )

        holding_times = []

        for o in closed_today:
            if o.get("filled_at") and o.get("closed_at"):
                try:
                    filled = datetime.fromisoformat(
                        o["filled_at"].replace("Z", "+00:00")
                    )

                    closed = datetime.fromisoformat(
                        o["closed_at"].replace("Z", "+00:00")
                    )

                    holding_times.append(
                        (closed - filled).total_seconds()
                    )

                except (ValueError, TypeError):
                    pass

        avg_hold_sec = (
            sum(holding_times) / len(holding_times)
            if holding_times
            else 0.0
        )

        lines = [
            "📊 *Today's Summary*",
            f"Date: `{today_str}`",
            "",
            f"Today's Trades: `{total}`",
            f"Wins: `{win_count}`  Losses: `{loss_count}`",
            f"Win Rate: `{win_rate:.1f}%`",
            f"Today's PnL: `{fmt_compact_number(today_pnl)}`",
            f"Today's ROI: `{today_roi:+.2f}%`",
        ]

        if win_count > 0:
            lines.append(
                f"Average Win: `{fmt_compact_number(avg_win)}`"
            )

        if loss_count > 0:
            lines.append(
                f"Average Loss: `{fmt_compact_number(avg_loss)}`"
            )

        if profit_factor > 0:
            lines.append(
                f"Profit Factor: `{profit_factor:.2f}`"
            )

        if avg_hold_sec > 0:
            lines.append(
                f"Average Holding Time: `{fmt_holding(avg_hold_sec)}`"
            )

        if best:
            lines.append(
                f"Best Trade Today: `{best.get('symbol', '?')}` "
                f"`{fmt_compact_number(best.get('net_pnl', 0))}`"
            )

        if worst:
            lines.append(
                f"Worst Trade Today: `{worst.get('symbol', '?')}` "
                f"`{fmt_compact_number(worst.get('net_pnl', 0))}`"
            )

        return "\n".join(lines)
