from telegram.base_command import BaseCommand, CommandMeta

# Exchanges known to use a quote currency other than USDT by default.
# Used only to warn the user — never assumed/auto-applied, since guessing
# wrong here would silently change what gets scanned.
_NON_USDT_EXCHANGES = {"indodax": "IDR"}


class ExchangeCommand(BaseCommand):
    meta = CommandMeta(
        name="exchange",
        aliases=["ex"],
        description="Show active exchange or switch to another one",
        usage="/exchange [name] [quote_currency]",
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
                f"Quote: `{mgr.quote_currency}`\n"
                f"\n"
                f"Use `/exchange <name> [quote]` to switch or "
                f"`/exchanges` to list all."
                "Use /exchange <name> to switch or /exchanges to list all accessible exchanges."
            )

        name = parts[0].lower()
        quote = parts[1].upper() if len(parts) > 1 else None
        try:
            mgr.set_active(name)
            if quote:
                mgr.set_quote_currency(quote)
            msg = (
                f"\u2705 Switched active exchange to `{name}`"
                f" (quote: `{mgr.quote_currency}`)."
            )
            expected_quote = _NON_USDT_EXCHANGES.get(name)
            if expected_quote and mgr.quote_currency != expected_quote:
                msg += (
                    f"\n\u26a0\ufe0f `{name}` normally trades in "
                    f"`{expected_quote}`, not `{mgr.quote_currency}` — "
                    f"the scanner will find 0 pairs like this. "
                    f"Run `/exchange {name} {expected_quote}` to fix it."
                )
            return msg
        except KeyError:
            supported = ", ".join(mgr.list_providers())
            return (
                f"\u274c Unsupported exchange: `{name}`.\n"
                f"Supported: {supported}"
            )
        except Exception as e:
            return f"\u274c Failed to switch exchange: {e}"
