from telegram.base_command import BaseCommand, CommandMeta


class ExchangeCommand(BaseCommand):
    meta = CommandMeta(
        name="exchange",
        aliases=["ex"],
        description="Show active exchange or switch to another one",
        usage="/exchange [name]",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        if ctx.services is None:
            return "Error: services not available."
        mgr = ctx.services.exchange
        parts = args.strip().split()

        if not parts:
            current = mgr.name
            return (
                f"\U0001f4e1 *Active Exchange*\n"
                f"Current: `{current}`\n"
                f"\n"
                f"Use `/exchange <name>` to switch or `/exchanges` to list all."
            )

        name = parts[0].lower()
        try:
            mgr.set_active(name)
            return f"\u2705 Switched active exchange to `{name}`."
        except KeyError:
            supported = ", ".join(mgr.list_providers())
            return (
                f"\u274c Unsupported exchange: `{name}`.\n"
                f"Supported: {supported}"
            )
        except Exception as e:
            return f"\u274c Failed to switch exchange: {e}"
