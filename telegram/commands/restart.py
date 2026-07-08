from telegram.base_command import BaseCommand, CommandMeta


class RestartCommand(BaseCommand):
    meta = CommandMeta(
        name="restart",
        aliases=["reboot"],
        description="Restart the bot process (placeholder)",
        usage="/restart",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f504 *Restart*\nRestart not implemented yet."
