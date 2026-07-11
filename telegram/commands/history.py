from datetime import datetime, timezone
from typing import Optional

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_price, fmt_pct, fmt_holding, time_ago


class HistoryCommand(BaseCommand):
    meta = CommandMeta(
        name="history",
        aliases=["trades", "closed"],
        description="Show last completed trades",
        usage="/history [count=10]",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        try:
            limit = max(1, min(50, int(args.strip())))
        except (ValueError, TypeError):
            limit = 10

        orders_data = ctx.read_json("paper_orders.json")
        state_data = ctx.read_json("state.json")

        closed_orders = []
        if orders_data:
            for o in orders_data.get("orders", []):
                if o.get("status") == "CLOSED":
                    closed_orders.append(o)
        if not closed_orders and state_data:
            trades = state_data.get("paper", {}).get("trades", [])
            for t in trades:
                closed_orders.append({
                    "symbol": t.get("symbol", "?"),
                    "side": "BUY",
                    "net_pnl": t.get("net_pnl", 0),
                    "net_pnl_pct": t.get("pnl_pct", 0),
                    "entry_price": t.get("entry_price", 0),
                    "exit_price": t.get("exit_price", 0),
                    "closed_at": t.get("exit_time", ""),
                    "filled_at": t.get("entry_time", ""),
                    "exit_reason": t.get("exit_reason", ""),
                    "total_cost": t.get("entry_price", 0) * t.get("quantity", 0),
                })

        if not closed_orders:
            return "No completed trades yet."

        closed_orders.sort(key=lambda o: o.get("closed_at", "") or "", reverse=True)
        shown = closed_orders[:limit]

        chunks = [f"\U0001f4cb *Trade History* (last {len(shown)})"]
        for o in shown:
            symbol = o.get("symbol", "?")
            entry = o.get("entry_price", 0)
            exit_p = o.get("exit_price", 0)
            pnl = o.get("net_pnl", 0)
            pnl_pct = o.get("net_pnl_pct", 0)
            reason = o.get("exit_reason", "?")
            closed_at = o.get("closed_at", "")

            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

            # Holding time
            hold = ""
            filled = o.get("filled_at", "")
            closed = o.get("closed_at", "")
            if filled and closed:
                try:
                    fdt = datetime.fromisoformat(filled.replace("Z", "+00:00"))
                    cdt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    hold_sec = (cdt - fdt).total_seconds()
                    hold = fmt_holding(hold_sec)
                except (ValueError, TypeError):
                    pass

            # PnL% from prices
            if entry > 0 and exit_p > 0:
                roi_pct = ((exit_p - entry) / entry * 100)
            else:
                roi_pct = float(pnl_pct) if pnl_pct else 0.0

            result = "\U0001f7e2 WIN" if pnl >= 0 else "\U0001f534 LOSS"

            time_str = time_ago(closed_at)
            buy_time = time_ago(filled) if filled else time_str

            chunks.append(
                f"{emoji} *{symbol}*\n"
                f"BUY: `{buy_time}`  SELL: `{time_str}`\n"
                f"Entry: `{fmt_price(entry)}`  Exit: `{fmt_price(exit_p)}`\n"
                f"Holding: `{hold}`  PnL: `${pnl:+,.2f}` ({pnl_pct:+.2f}%)\n"
                f"ROI: `{roi_pct:+.2f}%`  Reason: `{reason}`  Result: {result}"
            )

        # Summary
        pnls = [o.get("net_pnl", 0) for o in closed_orders]
        wins = [p for p in pnls if p >= 0]
        losses = [p for p in pnls if p < 0]
        avg_hold_sec = 0.0
        hold_count = 0
        for o in closed_orders:
            filled = o.get("filled_at", "")
            closed = o.get("closed_at", "")
            if filled and closed:
                try:
                    fdt = datetime.fromisoformat(filled.replace("Z", "+00:00"))
                    cdt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    avg_hold_sec += (cdt - fdt).total_seconds()
                    hold_count += 1
                except (ValueError, TypeError):
                    pass
        avg_hold_sec = avg_hold_sec / hold_count if hold_count > 0 else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        chunks.append(
            f"\n*Summary ({len(closed_orders)} total)*\n"
            f"Average Hold: `{fmt_holding(avg_hold_sec)}`\n"
            f"Average ROI: `{(sum(pnls) / len(pnls) if pnls else 0):+.2f}%`\n"
            f"Average Win: `${avg_win:+,.2f}`\n"
            f"Average Loss: `${avg_loss:+,.2f}`"
        )

        return "\n\n".join(chunks)
