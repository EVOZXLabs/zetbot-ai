from typing import Any


ALLOWED_CHAT_ID: str | None = None


def configure(chat_id: str) -> None:
    global ALLOWED_CHAT_ID
    ALLOWED_CHAT_ID = chat_id


def is_authorized(ctx: Any) -> bool:
    if ctx.test_mode:
        return True
    return str(ctx.chat_id) == ALLOWED_CHAT_ID
