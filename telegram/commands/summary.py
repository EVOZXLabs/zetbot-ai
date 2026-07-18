import csv
from datetime import datetime, timezone

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_holding
from telegram.ui import (
    header, SEPARATOR, pnl_emoji, confidence_bar, build_message,
)


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

        trades = []
        try:
            with open("data/paper_trade_history.csv", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    closed = row.get("closed_at", "") or ""
                    if today_str in closed:
                        row["net_pnl"] = float(row.get("net_pnl", 0) or 0)
                        row["net_pnl_pct"] = float(row.get("net_pnl_pct", 0) or 0)
                        trades.append(row)
        except FileNotFoundError:
            trades = []

        executions = trades
        positions = {}
        for trade in executions:
            trade_id = trade.get("id", "")
            if "-" in trade_id:
                position_id = "-".join(trade_id.split("-")[:2])
            else:
                position_id = trade_id
            positions[position_id] = positions.get(position_id, 0) + trade.get("net_pnl", 0)

        position_results = list(positions.values())
        wins = [pnl for pnl in position_results if pnl >= 0]
        losses = [pnl for pnl in position_results if pnl < 0]

        positions_closed = len(position_results)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / positions_closed * 100) if positions_closed > 0 else 0.0
        today_pnl = sum(position_results)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

        if not executions:
            return build_message(
                header(),
                f"📅 *DAILY SUMMARY*\n{SEPARATOR}\n{today_str}",
                "No trades completed today.",
            )

        blocks = [
            header(),
            f"📅 *DAILY SUMMARY*\n{SEPARATOR}\n{today_str}",
            f"📊 Positions: {positions_closed}\n"
            f"✅ Wins: {win_count}  ❌ Losses: {loss_count}\n"
            f"📈 Win Rate: {confidence_bar(win_rate)}",
            f"{SEPARATOR}\n"
            f"💰 PnL: {fmt_compact_number(today_pnl)}\n"
            f"📐 Profit Factor: {pf:.2f}",
        ]

        if avg_hold := self._avg_holding(executions):
            blocks.append(f"🕒 Avg Hold: {avg_hold}")

        return f"\n━━━━━━━━━━━━━━━━━━\n\n".join(blocks)

    @staticmethod
    def _avg_holding(executions: list) -> str:
        holding_times = []
        for t in executions:
            if t.get("filled_at") and t.get("closed_at"):
                try:
                    filled = datetime.fromisoformat(t["filled_at"].replace("Z", "+00:00"))
                    closed = datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00"))
                    holding_times.append((closed - filled).total_seconds())
                except (ValueError, TypeError):
                    pass
        if not holding_times:
            return ""
        avg = sum(holding_times) / len(holding_times)
        return fmt_holding(avg)
