import os

from telegram.base_command import BaseCommand, CommandMeta

PAUSE_FILE = "data/.paused"


class ResumeCommand(BaseCommand):
    meta = CommandMeta(
        name="resume",
        aliases=["r", "unpause"],
        description="Enable new trade openings",
        usage="/resume",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        return "\u25b6\ufe0f *Trading Resumed*\nNew trade openings enabled."
