from telegram.base_command import BaseCommand, CommandMeta


class ReloadCommand(BaseCommand):
    meta = CommandMeta(
        name="reload",
        aliases=["refresh"],
        description="Re-validate .env configuration and report effective values",
        usage="/reload",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        # AppConfig is a frozen dataclass consumed by every service at
        # startup — live-mutating it mid-run could leave the exchange
        # manager and the pipeline disagreeing about the same parameter.
        # What /reload genuinely does: re-read .env, re-validate every
        # value through the same loader the bot boots with, and show the
        # operator which settings will change after /restart.
        try:
            from scripts.app_config import load_config

            fresh = load_config()
        except Exception as exc:
            return f"\u274c *Reload FAILED*\n`{exc}`"

        cfg = ctx.config
        changes = []
        for key in ("exchange", "timeframe", "quote_currency", "paper_mode"):
            old = getattr(cfg, key, None)
            new = getattr(fresh, key, None)
            if old != new:
                changes.append(f"{key}: `{old}` → `{new}`")

        head = (
            f"\U0001f504 *Reload*\n"
            f"\u2705 .env validated (mode "
            f"{'PAPER' if fresh.paper_mode else 'LIVE'}), exchange "
            f"`{fresh.exchange}`, timeframe `{fresh.timeframe}`."
        )
        if changes:
            head += (
                "\n\n\U0001f4a1 Changes detected — take effect after "
                "/restart:\n" + "\n".join("- " + c for c in changes)
            )
        else:
            head += "\n\n\U0001f550 No changes — running config is current."
        return head