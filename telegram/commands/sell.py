from telegram.base_command import BaseCommand, CommandMeta


class SellCommand(BaseCommand):
    meta = CommandMeta(
        name="sell",
        aliases=["short", "close"],
        description="Manually close a position (placeholder)",
        usage="/sell <symbol>",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f6ab *Sell*\nManual sell not implemented yet."
