from telegram.base_command import BaseCommand, CommandMeta


class ReloadCommand(BaseCommand):
    meta = CommandMeta(
        name="reload",
        aliases=["refresh"],
        description="Reload bot configuration (placeholder)",
        usage="/reload",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        return "\U0001f504 *Reload*\nConfig reload not implemented yet."
