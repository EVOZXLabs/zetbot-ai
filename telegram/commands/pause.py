import datetime
import os

from telegram.base_command import BaseCommand, CommandMeta

PAUSE_FILE = "data/.paused"


class PauseCommand(BaseCommand):
    meta = CommandMeta(
        name="pause",
        aliases=["p"],
        description="Disable new trade openings",
        usage="/pause",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        os.makedirs("data", exist_ok=True)
        with open(PAUSE_FILE, "w") as f:
            f.write(datetime.datetime.now(datetime.timezone.utc).isoformat())
        return "\u23f8\ufe0f *Trading Paused*\nNew trades will not be opened.\nExisting positions continue to be managed."
