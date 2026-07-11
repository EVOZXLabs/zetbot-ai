from datetime import datetime, timezone
from typing import Optional

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_holding


class PerformanceCommand(BaseCommand):
    meta = CommandMeta(
        name="performance",
        aliases=["perf"],
        description="Trading performance metrics",
        usage="/performance",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        pb = ctx.read_json("paper_balance.json")
        orders_data = ctx.read_json("paper_orders.json")

        all_closed = []
        if orders_data:
            for o in orders_data.get("orders", []):
                if o.get("status") == "CLOSED":
                    all_closed.append(o)

        if not all_closed:
            return "No completed trades yet."

        def _parse_dt(s: str) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        # Group by period
        now = datetime.now(timezone.utc)
        daily = []
        weekly = []
        monthly = []

        for o in all_closed:
            dt = _parse_dt(o.get("closed_at", ""))
            if not dt:
                continue
            if dt.date() == now.date():
                daily.append(o)
            week_start = now.date().isocalendar()
            o_week = dt.date().isocalendar()
            if week_start[0] == o_week[0] and week_start[1] == o_week[1]:
                weekly.append(o)
            if dt.month == now.month and dt.year == now.year:
                monthly.append(o)

        def _metrics(orders: list) -> dict:
            pnls = [o.get("net_pnl", 0) for o in orders]
            total = len(pnls)
            wins = [p for p in pnls if p >= 0]
            losses = [p for p in pnls if p < 0]
            win_count = len(wins)
            loss_count = len(losses)
            wr = (win_count / total * 100) if total > 0 else 0.0
            gp = sum(wins)
            gl = abs(sum(losses))
            pf = (gp / gl) if gl > 0 else (gp if gp > 0 else 0.0)
            avg_w = gp / win_count if win_count > 0 else 0.0
            avg_l = gl / loss_count if loss_count > 0 else 0.0
            largest_w = max(wins) if wins else 0.0
            largest_l = min(losses) if losses else 0.0

            # Expectancy
            expectancy = ((wr / 100) * avg_w) - (((100 - wr) / 100) * avg_l) if total > 0 else 0.0

            # Holding time
            hold_times = []
            for o in orders:
                filled = _parse_dt(o.get("filled_at", ""))
                closed = _parse_dt(o.get("closed_at", ""))
                if filled and closed:
                    hold_times.append((closed - filled).total_seconds())
            avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0.0

            # Streaks
            sorted_orders = sorted(orders, key=lambda o: o.get("closed_at", ""))
            cur_win_streak = 0
            cur_loss_streak = 0
            max_win_streak = 0
            max_loss_streak = 0
            for o in sorted_orders:
                if o.get("net_pnl", 0) >= 0:
                    cur_win_streak += 1
                    cur_loss_streak = 0
                    max_win_streak = max(max_win_streak, cur_win_streak)
                else:
                    cur_loss_streak += 1
                    cur_win_streak = 0
                    max_loss_streak = max(max_loss_streak, cur_loss_streak)

            return {
                "trades": total,
                "win_rate": wr,
                "profit_factor": pf,
                "expectancy": expectancy,
                "avg_win": avg_w,
                "avg_loss": avg_l,
                "largest_win": largest_w,
                "largest_loss": largest_l,
                "cur_win_streak": cur_win_streak,
                "cur_loss_streak": cur_loss_streak,
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "avg_holding": avg_hold,
            }

        def _fmt_period(m: dict, label: str) -> str:
            return (
                f"*{label}*\n"
                f"Trades: `{m['trades']}`  Win Rate: `{m['win_rate']:.1f}%`\n"
                f"Profit Factor: `{m['profit_factor']:.2f}`  Expectancy: `{m['expectancy']:+.2f}`\n"
                f"Avg Win: `{fmt_compact_number(m['avg_win'])}`  "
                f"Avg Loss: `{fmt_compact_number(m['avg_loss'])}`\n"
                f"Largest Win: `{fmt_compact_number(m['largest_win'])}`  "
                f"Largest Loss: `{fmt_compact_number(m['largest_loss'])}`\n"
                f"Win Streak: `{m['cur_win_streak']}` (max `{m['max_win_streak']}`)  "
                f"Loss Streak: `{m['cur_loss_streak']}` (max `{m['max_loss_streak']}`)\n"
                f"Avg Holding: `{fmt_holding(m['avg_holding'])}`"
            )

        daily_m = _metrics(daily)
        weekly_m = _metrics(weekly)
        monthly_m = _metrics(monthly)

        lines = ["\U0001f4c8 *Performance*"]
        if daily_m["trades"] > 0:
            lines.append("")
            lines.append(_fmt_period(daily_m, "Daily"))
        if weekly_m["trades"] > 0:
            lines.append("")
            lines.append(_fmt_period(weekly_m, "Weekly"))
        if monthly_m["trades"] > 0:
            lines.append("")
            lines.append(_fmt_period(monthly_m, "Monthly"))

        if not lines:
            lines.append("No trades completed yet.")

        return "\n".join(lines)
