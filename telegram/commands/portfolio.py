import os

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, fmt_price, fmt_pct


PAUSE_FILE = "data/.paused"


class PortfolioCommand(BaseCommand):
    meta = CommandMeta(
        name="portfolio",
        aliases=["pf"],
        description="Show portfolio overview",
        usage="/portfolio",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        pb = ctx.read_json("paper_balance.json")
        pos_data = ctx.read_json("positions.json")
        health_snapshot = {}
        if ctx.health_monitor:
            try:
                health_snapshot = ctx.health_monitor.snapshot()
            except Exception:
                pass

        mode = "PAPER" if ctx.config.paper_mode else "LIVE"

        cash = pb.get("final_balance", 0.0) if pb else 0.0
        equity = pb.get("final_equity", 0.0) if pb else 0.0
        net_pnl = pb.get("net_pnl", 0.0) if pb else 0.0
        realized = pb.get("realized_pnl", 0.0) if pb else 0.0
        unrealized = pb.get("unrealized_pnl", 0.0) if pb else 0.0
        total_trades = pb.get("total_trades", 0) if pb else 0
        win_rate = pb.get("win_rate", 0.0) if pb else 0.0

        pos_list = pos_data.get("positions", []) if pos_data else []
        open_positions = [p for p in pos_list if p.get("status") == "OPEN"]
        pos_value = sum(p.get("position_size_usdt", 0) for p in open_positions)
        exposure_pct = (pos_value / equity * 100) if equity > 0 else 0.0
        open_count = len(open_positions)

        today_pnl = 0.0
        orders_data = ctx.read_json("paper_orders.json")
        if orders_data:
            import datetime
            today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            for o in orders_data.get("orders", []):
                closed = o.get("closed_at", "") or ""
                if today_str in closed and o.get("status") == "CLOSED":
                    today_pnl += o.get("net_pnl", 0)

        paused = os.path.exists(PAUSE_FILE)
        health_score = health_snapshot.get("score", "N/A")
        hs = f"{health_score:.0f}" if isinstance(health_score, (int, float)) else "N/A"

        return (
            f"\U0001f911 *Portfolio*\n"
            f"Mode: `{mode}`\n"
            f"Cash: `{fmt_compact_number(cash)}`\n"
            f"In Position: `{fmt_compact_number(pos_value)}`\n"
            f"Equity: `{fmt_compact_number(equity)}`\n"
            f"Net PnL: `{net_pnl:+,.2f}`  Today: `{today_pnl:+,.2f}`\n"
            f"Exposure: `{exposure_pct:.1f}%`  Open: `{open_count}`\n"
            f"Win Rate: `{win_rate:.1f}%` (`{total_trades}` trades)\n"
            f"Health: `{hs}`"
        )
