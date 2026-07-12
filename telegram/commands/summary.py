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

        trades = []

        try:
            with open("data/paper_trade_history.csv", newline="") as f:
                reader = csv.DictReader(f)

                for row in reader:
                    closed = row.get("closed_at", "") or ""

                    if today_str in closed:
                        row["net_pnl"] = float(
                            row.get("net_pnl", 0) or 0
                        )

                        row["net_pnl_pct"] = float(
                            row.get("net_pnl_pct", 0) or 0
                        )

                        trades.append(row)

        except FileNotFoundError:
            trades = []

        # Exit executions (TP1/TP2/TP3)
        executions = trades

        # Group position by original trade timestamp.
        # Example:
        # PAPER-20260712044244-0010
        # PAPER-20260712044244-0011
        # PAPER-20260712044244-0012
        #
        # becomes 1 position
        positions = {}

        for trade in executions:
            trade_id = trade.get("id", "")

            if "-" in trade_id:
                position_id = "-".join(
                    trade_id.split("-")[:2]
                )
            else:
                position_id = trade_id

            positions[position_id] = positions.get(
                position_id,
                0
            ) + trade.get("net_pnl", 0)

        position_results = list(positions.values())

        wins = [
            pnl for pnl in position_results
            if pnl >= 0
        ]

        losses = [
            pnl for pnl in position_results
            if pnl < 0
        ]

        positions_closed = len(position_results)
        exit_executions = len(executions)

        win_count = len(wins)
        loss_count = len(losses)

        win_rate = (
            win_count / positions_closed * 100
            if positions_closed > 0
            else 0.0
        )

        today_pnl = sum(
            position_results
        )

        gross_profit = sum(wins)

        gross_loss = abs(
            sum(losses)
        )

        if gross_loss > 0:
            profit_factor = (
                gross_profit / gross_loss
            )
        else:
            profit_factor = None

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
                executions,
                key=lambda x: x.get("net_pnl", 0)
            )
            if executions
            else None
        )

        worst = (
            min(
                executions,
                key=lambda x: x.get("net_pnl", 0)
            )
            if executions
            else None
        )

        total_cost = sum(
            float(t.get("quantity", 0))
            *
            float(t.get("entry_price", 0))
            for t in executions
        )

        today_roi = (
            today_pnl / total_cost * 100
            if total_cost > 0
            else 0.0
        )

        holding_times = []

        for t in executions:
            if t.get("filled_at") and t.get("closed_at"):
                try:
                    filled = datetime.fromisoformat(
                        t["filled_at"].replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    closed = datetime.fromisoformat(
                        t["closed_at"].replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    holding_times.append(
                        (
                            closed - filled
                        ).total_seconds()
                    )

                except (
                    ValueError,
                    TypeError
                ):
                    pass

        avg_hold_sec = (
            sum(holding_times)
            /
            len(holding_times)
            if holding_times
            else 0
        )

        lines = [
            "📊 *Today's Summary*",
            f"Date: `{today_str}`",
            "",
            f"Positions Closed: `{positions_closed}`",
            f"Exit Executions: `{exit_executions}`",
            "",
            f"Wins: `{win_count}`  Losses: `{loss_count}`",
            f"Win Rate: `{win_rate:.1f}%`",
            "",
            f"Today's PnL: `{fmt_compact_number(today_pnl)}`",
            f"Today's ROI: `{today_roi:+.2f}%`",
        ]

        if win_count:
            lines.append(
                f"Average Win: `{fmt_compact_number(avg_win)}`"
            )

        if loss_count:
            lines.append(
                f"Average Loss: `{fmt_compact_number(avg_loss)}`"
            )

        if profit_factor is not None:
            lines.append(
                f"Profit Factor: `{profit_factor:.2f}`"
            )
        else:
            lines.append(
                "Profit Factor: `N/A (no losses)`"
            )

        if avg_hold_sec:
            lines.append(
                f"Average Holding Time: `{fmt_holding(avg_hold_sec)}`"
            )

        if best:
            lines.append(
                f"Best Exit: `{best.get('symbol', '?')}` "
                f"`{fmt_compact_number(best.get('net_pnl', 0))}`"
            )

        if worst:
            lines.append(
                f"Worst Exit: `{worst.get('symbol', '?')}` "
                f"`{fmt_compact_number(worst.get('net_pnl', 0))}`"
            )

        return "\n".join(lines)
