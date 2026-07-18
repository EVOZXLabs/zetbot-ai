from datetime import datetime, timezone
from typing import Optional

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_holding
from telegram.ui import header, SEPARATOR, progress_bar, pnl_emoji, build_message


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
            return f"{header()}\n\nNo completed trades yet."

        def _parse_dt(s: str) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

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

            expectancy = ((wr / 100) * avg_w) - (((100 - wr) / 100) * avg_l) if total > 0 else 0.0

            hold_times = []
            for o in orders:
                filled = _parse_dt(o.get("filled_at", ""))
                closed = _parse_dt(o.get("closed_at", ""))
                if filled and closed:
                    hold_times.append((closed - filled).total_seconds())
            avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0.0

            return {
                "trades": total,
                "win_rate": wr,
                "profit_factor": pf,
                "expectancy": expectancy,
                "avg_win": avg_w,
                "avg_loss": avg_l,
                "avg_holding": avg_hold,
            }

        daily_m = _metrics(daily)
        weekly_m = _metrics(weekly)
        monthly_m = _metrics(monthly)

        blocks = [header(), f"📈 *PERFORMANCE*\n{SEPARATOR}"]

        def _fmt_period(m: dict, label: str) -> str:
            if m["trades"] == 0:
                return ""
            return (
                f"*{label}*\n"
                f"Trades: {m['trades']}  WR: {m['win_rate']:.0f}%\n"
                f"PF: {m['profit_factor']:.2f}  E: {m['expectancy']:+.2f}\n"
                f"Avg Win: {fmt_compact_number(m['avg_win'])}  "
                f"Avg Loss: {fmt_compact_number(m['avg_loss'])}\n"
                f"Avg Hold: {fmt_holding(m['avg_holding'])}"
            )

        for period_m, label in [
            (daily_m, "📅 Today"),
            (weekly_m, "📆 This Week"),
            (monthly_m, "🗓 This Month"),
        ]:
            text = _fmt_period(period_m, label)
            if text:
                blocks.append(text)

        return f"\n━━━━━━━━━━━━━━━━━━\n\n".join(blocks)
