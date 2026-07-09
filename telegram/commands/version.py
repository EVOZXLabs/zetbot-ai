import datetime
import os
import subprocess
import sys
import time

from telegram.base_command import BaseCommand, CommandMeta

BOT_VERSION = "v0.5.0"


class VersionCommand(BaseCommand):
    meta = CommandMeta(
        name="version",
        aliases=["v", "ver"],
        description="Show ZetBot version information",
        usage="/version",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        git_commit = "N/A"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                git_commit = result.stdout.strip()
        except Exception:
            pass

        py_ver = sys.version.split()[0]
        exchange_name = ctx.config.exchange
        mode = "PAPER" if ctx.config.paper_mode else "LIVE"

        uptime_sec = time.time() - ctx.start_time
        days, rem = divmod(int(uptime_sec), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hours:02d}h {minutes:02d}m" if days else f"{hours:02d}h {minutes:02d}m"

        start_time_str = datetime.datetime.fromtimestamp(
            ctx.start_time, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"\U0001f916 *ZetBot AI*\n"
            f"Version: `{BOT_VERSION}`\n"
            f"Git: `{git_commit}`\n"
            f"Python: `{py_ver}`\n"
            f"Exchange: `{exchange_name}`\n"
            f"Mode: `{mode}`\n"
            f"Started: `{start_time_str}`\n"
            f"Uptime: `{uptime_str}`"
        )
