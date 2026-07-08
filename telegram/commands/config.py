from telegram.base_command import BaseCommand, CommandMeta


class ConfigCommand(BaseCommand):
    meta = CommandMeta(
        name="config",
        aliases=["cfg", "settings"],
        description="Show current bot configuration",
        usage="/config",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        cfg = ctx.config
        return (
            f"\U0001f527 *Configuration*\n"
            f"Exchange: `{cfg.exchange}`\n"
            f"Mode: `{'PAPER' if cfg.paper_mode else 'LIVE'}`\n"
            f"Symbols: `{getattr(cfg, 'symbols', 'N/A')}`\n"
            f"Timeframe: `{getattr(cfg, 'timeframe', 'N/A')}`\n"
            f"Scanner threads: `{getattr(cfg, 'scanner_threads', 'N/A')}`\n"
            f"OHLCV limit: `{getattr(cfg, 'ohlcv_limit', 'N/A')}`"
        )
