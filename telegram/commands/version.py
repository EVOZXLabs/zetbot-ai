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

        if ctx.services is not None:
            uptime_sec = time.time() - ctx.services.daemon_start_time
        else:
            uptime_sec = time.time() - ctx.start_time
        days, rem = divmod(int(uptime_sec), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hours:02d}h {minutes:02d}m" if days else f"{hours:02d}h {minutes:02d}m"

        if ctx.services is not None:
            start_time_ts = ctx.services.daemon_start_time
        else:
            start_time_ts = ctx.start_time
        start_time_str = datetime.datetime.fromtimestamp(
            start_time_ts, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Estimate build time from git commit timestamp
        build_time = "N/A"
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ci"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                build_time = result.stdout.strip()
        except Exception:
            pass

        return (
            f"\U0001f916 *ZetBot AI*\n"
            f"Version: `{BOT_VERSION}`\n"
            f"Git: `{git_commit}`\n"
            f"Build: `{build_time}`\n"
            f"Python: `{py_ver}`\n"
            f"Exchange: `{exchange_name}`\n"
            f"Mode: `{mode}`\n"
            f"Started: `{start_time_str}`\n"
            f"Uptime: `{uptime_str}`"
        )
