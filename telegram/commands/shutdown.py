import os
import time

from telegram.base_command import BaseCommand, CommandMeta

SHUTDOWN_FILE = "data/.shutdown_requested"
STARTUP_GRACE_PERIOD = 30
DATA_DIR = "data"


class ShutdownCommand(BaseCommand):
    meta = CommandMeta(
        name="shutdown",
        aliases=["stop", "exit", "quit"],
        description="Gracefully shut down the bot",
        usage="/shutdown",
        permission="admin",
    )

    # State per-instance is not great; use class-level
    _pending: dict[str, float] = {}

    def execute(self, ctx, args: str) -> str:
        now = time.time()

        # Grace period to prevent replay
        if not ctx.test_mode and now - ctx.start_time < STARTUP_GRACE_PERIOD:
            remaining = int(STARTUP_GRACE_PERIOD - (now - ctx.start_time))
            return (
                "\u26a0\ufe0f *Shutdown ignored*\n"
                f"Bot just started. Try again in {remaining}s."
            )

        key = str(ctx.chat_id)
        last_request = self._pending.get(key, 0.0)

        if not last_request or (now - last_request > 60.0):
            self._pending[key] = now
            return (
                "\u26a0\ufe0f *Shutdown Confirmation*\n"
                "Are you sure? Send `/shutdown` again within 60 seconds "
                "to confirm and shut down the bot."
            )

        # Confirm
        if ctx.shutdown_event:
            ctx.shutdown_event.set()
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            import datetime  # noqa: PLC0415
            with open(SHUTDOWN_FILE, "w") as f:
                f.write(datetime.datetime.now(datetime.timezone.utc).isoformat())

        self._pending.pop(key, None)
        return (
            "\U0001f6d1 *Shutting Down*\n"
            "Graceful shutdown initiated. Goodbye."
        )
