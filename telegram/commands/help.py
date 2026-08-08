from telegram.base_command import BaseCommand, CommandMeta
from telegram.registry import CommandRegistry
from telegram.ui import compact_header, build_message

_MD_SPECIAL = ("_", "*", "`", "[")


def _esc(text: str) -> str:
    for ch in _MD_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


_CATEGORY_RULES: list[tuple[str, set[str]]] = [
    ("📊 Trading", {"status", "positions", "signals", "signal", "detail",
                    "history", "portfolio", "performance", "wallet",
                    "summary", "market", "buy", "sell", "live"}),
    ("🩺 Monitoring", {"health", "scan", "pipeline", "version", "logs"}),
    ("💰 Account", {"balance", "exchange", "exchanges"}),
    ("⚙️ System", {"help", "pause", "resume", "config", "reload",
                   "restart", "shutdown"}),
]


def _categorize(name: str) -> str:
    for category, names in _CATEGORY_RULES:
        if name in names:
            return category
    return "⚙️ System"


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
        all_meta = registry.get_all_commands()

        public_meta = [
            m for m in all_meta
            if not m.hidden and (m.permission != "admin" or ctx.is_admin)
        ]

        categories: dict[str, list[CommandMeta]] = {}
        for m in public_meta:
            cat = _categorize(m.name)
            categories.setdefault(cat, []).append(m)

        for cmds in categories.values():
            cmds.sort(key=lambda m: m.name)

        blocks = [compact_header(), "*Command Reference*"]
        for category, cmds in categories.items():
            rows = []
            for m in cmds:
                usage = _esc(m.usage or f"/{m.name}")
                desc = _esc(m.description or "")
                rows.append(f"{usage} — {desc}")
            if rows:
                blocks.append(f"{category}\n" + "\n".join(rows))

        blocks.append("_Tip: tap any command above to run it._")

        return build_message(*blocks)
