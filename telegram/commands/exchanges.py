from telegram.base_command import BaseCommand, CommandMeta


class ExchangesCommand(BaseCommand):
    meta = CommandMeta(
        name="exchanges",
        aliases=["exlist"],
        description="List all supported exchanges and their status",
        usage="/exchanges",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        if ctx.services is None:
            return "Error: services not available."
        mgr = ctx.services.exchange
        active = mgr.name
        connected = mgr.list_connected()

        lines = [
            "\U0001f310 *Supported Exchanges*",
            "",
        ]
        for name in sorted(connected):
            is_active = " \u2b50" if name == active else ""
            status = "\u2705" if connected[name] else "\u274c"
            lines.append(f"{status} `{name}`{is_active}")

        lines.append("")
        lines.append(f"Active: `{active}`")
        return "\n".join(lines)
