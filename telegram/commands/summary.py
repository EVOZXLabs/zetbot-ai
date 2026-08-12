from datetime import datetime, timezone

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import (
    fmt_compact_number, fmt_holding, fmt_pf, order_hold_seconds,
)
from telegram.ui import (
    compact_header, confidence_bar, build_message,
)
from scripts.metrics_manager import MetricsManager


class SummaryCommand(BaseCommand):
    meta = CommandMeta(
        name="summary",
        aliases=["overview", "report"],
        description="Today's trading statistics",
        usage="/summary",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        # Single source of truth for closed trades = MetricsManager
        # (paper_trade_history.csv).  /summary and /history therefore always
        # agree on the closed-trade set, win rate, realized PnL and
        # holding time — no more divergent CSV-vs-orders/legacy-state reads.
        if ctx.services is not None:
            manager = ctx.services.metrics
        else:
            manager = MetricsManager("data")
        # entry_time_map reads positions.json / paper_state.json directly, so
        # it works with or without a service container.
        entry_map = ctx.entry_time_map()

        quote = getattr(ctx.config, "quote_currency", "USDT") or "USDT"
        trades = manager.trades_since_wib_midnight()

        if not trades:
            return build_message(
                compact_header(),
                f"📅 *Daily Summary* — {_today_str()}",
                "No trades completed today.",
            )

        total = len(trades)
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total * 100.0
        today_pnl = sum(t.get("net_pnl", 0) for t in trades)
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (
            0.0 if gross_profit == 0 else 0.0
        )

        avg_hold = 0.0
        holds = []
        for t in trades:
            # Force the entry-time lookup to come from the position record
            # (entry_time_map) rather than the trade's own fill timestamp,
            # so the displayed hold reflects the REAL position holding time,
            # not "0s" derived from the closing order's fill time.
            exit_view = {
                "symbol": t.get("symbol", ""),
                "exit_time": t.get("exit_time") or t.get("closed_at", ""),
            }
            h = order_hold_seconds(exit_view, entry_map)
            if h is not None:
                holds.append(h)
        if holds:
            avg_hold = sum(holds) / len(holds)

        blocks = [
            compact_header(),
            f"📅 *Daily Summary* — {_today_str()}",
            f"📊 Positions: {total}\n"
            f"✅ Wins: {win_count}  ❌ Losses: {loss_count}\n"
            f"📈 Win Rate: {confidence_bar(win_rate)}",
            f"💰 PnL: {fmt_compact_number(today_pnl, quote)}\n"
            f"📐 Profit Factor: {fmt_pf(pf)}",
        ]

        if avg_hold:
            blocks.append(f"🕒 Avg Hold: {fmt_holding(avg_hold)}")

        return build_message(*blocks)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
