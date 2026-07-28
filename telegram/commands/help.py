from telegram.base_command import BaseCommand, CommandMeta
from telegram.registry import CommandRegistry
from telegram.ui import compact_header, build_message

# Markdown (legacy) special characters — anything pulled from a command's
# usage/description gets escaped before it's dropped into the message so
# a stray "_" (e.g. inside a placeholder like "amount_usdt") can never
# break formatting for the whole message.
_MD_SPECIAL = ("_", "*", "`", "[")


def _esc(text: str) -> str:
    for ch in _MD_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


class HelpCommand(BaseCommand):
    meta = CommandMeta(
        name="help",
        aliases=["start"],
        description="Show this help message",
        usage="/help",
        permission="user",
    )

    # icon, section title, command names (in display order)
    _SECTIONS = [
        ("📊", "Trading", ["status", "positions", "signals", "detail", "history",
                            "portfolio", "performance", "wallet", "summary", "market"]),
        ("🩺", "Monitoring", ["health", "scan", "pipeline", "version", "logs"]),
        ("💰", "Account", ["balance", "exchange", "exchanges"]),
        ("⚙️", "System", ["help", "pause", "resume", "config", "reload", "restart", "shutdown"]),
    ]

    def execute(self, ctx, args: str) -> str:
        registry = CommandRegistry()
        registry.discover()
        cmd_map = {m.name: m for m in registry.get_all_commands()}

        blocks = [compact_header(), "*Command Reference*"]

        for icon, title, names in self._SECTIONS:
            rows = []
            for name in names:
                meta = cmd_map.get(name)
                if not meta or meta.hidden:
                    continue
                if meta.permission == "admin" and not ctx.is_admin:
                    continue
                usage = _esc(meta.usage or f"/{meta.name}")
                desc = _esc(meta.description or "")
                rows.append(f"{usage} — {desc}")
            if rows:
                blocks.append(f"{icon} *{title}*\n" + "\n".join(rows))

        blocks.append("_Tip: tap any command above to run it._")

        return build_message(*blocks)
