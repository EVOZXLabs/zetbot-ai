import os

from telegram.base_command import BaseCommand, CommandMeta

LOG_DIR = "logs"


class LogsCommand(BaseCommand):
    meta = CommandMeta(
        name="logs",
        aliases=["log"],
        description="Show recent log output (last 20 lines)",
        usage="/logs [n]",
        permission="admin",
        examples=["/logs", "/logs 50"],
    )

    def execute(self, ctx, args: str) -> str:
        n = 20
        if args.strip().isdigit():
            n = int(args.strip())
        # Hard cap so a single /logs can never blow past Telegram's 4096
        # char message limit (replies are chunked, but a bounded reply is
        # cleaner than an unbounded stream of chunks).
        n = max(1, min(n, 100))

        if not os.path.isdir(LOG_DIR):
            return "No logs directory found."

        log_files = sorted(
            [f for f in os.listdir(LOG_DIR) if f.endswith(".log")],
            key=lambda f: os.path.getmtime(os.path.join(LOG_DIR, f)),
            reverse=True,
        )
        if not log_files:
            return "No log files found."

        latest = os.path.join(LOG_DIR, log_files[0])
        try:
            with open(latest) as f:
                lines = f.readlines()
            last_n = "".join(lines[-n:])
            return f"\U0001f4c4 *Recent Logs* (`{log_files[0]}`)\n```\n{last_n}\n```"
        except Exception as exc:
            return f"Failed to read logs: `{exc}`"
