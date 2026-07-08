from telegram.base_command import BaseCommand, CommandMeta


class SignalsCommand(BaseCommand):
    meta = CommandMeta(
        name="signals",
        aliases=["signal"],
        description="Show latest trading signals (placeholder)",
        usage="/signals",
        permission="user",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f4e1 *Signals*\nSignals display not implemented yet."
