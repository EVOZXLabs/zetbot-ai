from telegram.base_command import BaseCommand, CommandMeta
from telegram.registry import CommandRegistry


class HelpCommand(BaseCommand):
    meta = CommandMeta(
        name="help",
        aliases=["start"],
        description="Show this help message",
        usage="/help",
        permission="user",
    )

    _SECTIONS = {
        "Trading": ["status", "positions", "signals", "history", "portfolio", "performance", "wallet", "summary", "market"],
        "Monitoring": ["health", "scan", "pipeline", "version", "logs"],
        "Account": ["balance", "exchange", "exchanges"],
        "System": ["help", "pause", "resume", "config", "reload", "restart", "shutdown"],
    }

    def execute(self, ctx, args: str) -> str:
        registry = CommandRegistry()
        registry.discover()
        cmd_map = {m.name: m for m in registry.get_all_commands()}

        lines = ["Available Commands"]
        lines.append("")
        for section, names in self._SECTIONS.items():
            lines.append(section)
            for name in names:
                meta = cmd_map.get(name)
                if not meta or meta.hidden:
                    continue
                if meta.permission == "admin" and not ctx.is_admin:
                    continue
                usage = meta.usage or f"/{meta.name}"
                desc = meta.description or ""
                lines.append(f"{usage} — {desc}")
            lines.append("")

        return "\n".join(lines).strip()
