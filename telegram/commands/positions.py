from telegram.base_command import BaseCommand, CommandMeta

DATA_DIR = "data"


class PositionsCommand(BaseCommand):
    meta = CommandMeta(
        name="positions",
        aliases=["pos", "position"],
        description="Show all open positions with details",
        usage="/positions",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        pos_data = ctx.read_json("positions.json")
        pos_list = pos_data.get("positions", [])

        open_positions = [p for p in pos_list if p.get("status") == "OPEN"]

        if not open_positions:
            return "No open positions."

        chunks = []
        for p in open_positions:
            pnl = p.get("floating_pnl", 0)
            pnl_pct = p.get("floating_pnl_pct", 0)
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

            chunk = (
                f"{emoji} *{p['symbol']}*\n"
                f"Entry: `{p['entry_price']:.6f}`  "
                f"Curr: `{p['current_price']:.6f}`\n"
                f"PnL: `${pnl:+,.2f}` ({pnl_pct:+.2f}%)\n"
                f"SL: `{p.get('stop_loss', 0):.6f}`\n"
                f"TP1: `{p.get('tp1', 0):.6f}`  "
                f"TP2: `{p.get('tp2', 0):.6f}`  "
                f"TP3: `{p.get('tp3', 0):.6f}`\n"
                f"Holding: `{p.get('holding_hours', 0):.1f}h`  "
                f"Size: `${p.get('position_size_usdt', 0):,.2f}`"
            )
            chunks.append(chunk)

        return "\n\n".join(chunks)
