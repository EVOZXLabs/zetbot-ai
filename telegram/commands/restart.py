import os
import sys

from telegram.base_command import BaseCommand, CommandMeta


class RestartCommand(BaseCommand):
    meta = CommandMeta(
        name="restart",
        aliases=["reboot"],
        description="Restart the bot process now",
        usage="/restart",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        # Never in test mode: the command smoke tests execute every
        # command and must not replace the pytest process.
        if getattr(ctx, "test_mode", False):
            return "\u2139\ufe0f /restart is not available in test mode."

        # Exec replaces the current process image in-place (same PID) —
        # the watchdog sees no crash, state files (written atomically on
        # every pipeline cycle) are left untouched, and the freshly
        # started main() re-runs its normal startup recovery.
        argv = sys.argv[:] if sys.argv else [sys.executable, "main.py"]
        try:
            os.execv(sys.executable, [sys.executable] + argv)
        except Exception as exc:
            return f"\u274c Restart failed: `{exc}`"
        return "\U0001f504 *Restarting…*"  # unreachable in practice