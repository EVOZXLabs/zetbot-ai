from telegram.base_command import BaseCommand, CommandMeta


class BuyCommand(BaseCommand):
    meta = CommandMeta(
        name="buy",
        aliases=["long"],
        description="Manually open a position (placeholder)",
        usage="/buy <symbol> [amount]",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f6ab *Buy*\nManual buy not implemented yet."
