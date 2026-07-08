from telegram.base_command import BaseCommand, CommandMeta


class StoplossCommand(BaseCommand):
    meta = CommandMeta(
        name="stoploss",
        aliases=["sl"],
        description="Show or set stop-loss levels (placeholder)",
        usage="/stoploss [symbol] [price]",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f6ab *Stop-Loss*\nStop-loss management not implemented yet."
