from telegram.base_command import BaseCommand, CommandMeta
from telegram.command_center import CommandCenter


class HelpCommand(BaseCommand):
    meta = CommandMeta(
        name="help",
        aliases=["start"],
        description="Show this help message",
        usage="/help",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        cc = CommandCenter(ctx.config, ctx.logger)
        return cc.generate_help(user_is_admin=ctx.is_admin)
