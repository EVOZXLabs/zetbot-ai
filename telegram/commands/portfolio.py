from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    header, SEPARATOR, wib_now, exposure_bar, progress_bar,
    pnl_emoji, build_message,
)


class PortfolioCommand(BaseCommand):
    meta = CommandMeta(
        name="portfolio",
        aliases=["pf"],
        description="Show portfolio overview",
        usage="/portfolio",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        m = ctx.services.metrics if ctx.services else None

        if m is not None:
            a = m.account()
            cash = a.balance
            equity = a.equity
            net_pnl = a.net_pnl
            open_count = a.open_positions
            win_rate = a.win_rate
        else:
            pb = ctx.read_json("paper_balance.json")
            cash = pb.get("final_balance", 0.0) if pb else 0.0
            equity = pb.get("final_equity", 0.0) if pb else 0.0
            net_pnl = pb.get("net_pnl", 0.0) if pb else 0.0
            win_rate = pb.get("win_rate", 0.0) if pb else 0.0
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", []) if pos_data else []
            open_count = len([p for p in pos_list if p.get("status") == "OPEN"])

        pos_data = ctx.read_json("positions.json")
        pos_list = pos_data.get("positions", []) if pos_data else []
        open_positions = [p for p in pos_list if p.get("status") == "OPEN"]
        pos_value = sum(p.get("position_size_usdt", 0) for p in open_positions)
        exposure_pct = (pos_value / equity * 100) if equity > 0 else 0.0

        return build_message(
            header(),
            f"👛 *PORTFOLIO*\n{SEPARATOR}",
            f"Cash\n${cash:,.2f}",
            f"{SEPARATOR}\nEquity\n${equity:,.2f}",
            f"{SEPARATOR}\n"
            f"PnL\n{pnl_emoji(net_pnl)} ${net_pnl:+,.2f}",
            f"{SEPARATOR}\n"
            f"Exposure\n{exposure_bar(exposure_pct)}",
        )
