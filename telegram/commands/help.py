from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import bold, code
from telegram.registry import CommandRegistry


class HelpCommand(BaseCommand):
    meta = CommandMeta(
        name="help",
        aliases=["start"],
        description="Show this help message",
        usage="/help",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        registry = CommandRegistry()
        registry.discover()
        parts = [bold("Available Commands\n")]
        for meta in registry.get_all_commands():
            if meta.hidden:
                continue
            if meta.permission == "admin" and not ctx.is_admin:
                continue
            usage = meta.usage or f"/{meta.name}"
            parts.append(f"{code(usage)} — {meta.description}")
        return "\n".join(parts)
