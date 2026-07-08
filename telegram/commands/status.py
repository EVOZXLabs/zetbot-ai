import os

from telegram.base_command import BaseCommand, CommandMeta

PAUSE_FILE = "data/.paused"
DATA_DIR = "data"


class StatusCommand(BaseCommand):
    meta = CommandMeta(
        name="status",
        aliases=["stats"],
        description="Bot status, runtime, balance, positions overview",
        usage="/status",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        runtime = ctx.runtime_formatted()
        pb = ctx.read_json("paper_balance.json")
        pos_data = ctx.read_json("positions.json")
        pos_list = pos_data.get("positions", [])

        open_pos = sum(1 for p in pos_list if p.get("status") == "OPEN")
        closed_pos = sum(
            1 for p in pos_list if p.get("status") in ("CLOSED", "STOPPED", "TIMEOUT")
        )
        paused = os.path.exists(PAUSE_FILE)

        return (
            f"\U0001f916 *Bot Status*\n"
            f"Status: `ONLINE`\n"
            f"Exchange: `{ctx.config.exchange}`\n"
            f"Mode: `{'PAPER' if ctx.config.paper_mode else 'LIVE'}`\n"
            f"Runtime: `{runtime}`\n"
            f"Trading: `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`\n"
            f"Balance: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Equity: `${pb.get('final_equity', 0):,.2f}`\n"
            f"Cash: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Net PnL: `${pb.get('net_pnl', 0):+,.2f}`\n"
            f"Open Positions: `{open_pos}`\n"
            f"Closed Positions: `{closed_pos}`"
        )
