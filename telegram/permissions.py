from typing import Any

# A set rather than a single value — Telegram and WhatsApp (and any future
# channel) each call configure() with their own authorized chat/number.
# Using a single global used to mean the second channel to start would
# silently lock the first one out.
ALLOWED_CHAT_IDS: set[str] = set()

# Backward-compat alias — some older call sites may still read this.
ALLOWED_CHAT_ID: str | None = None


def configure(chat_id: str) -> None:
    """Register an authorized chat/number. Safe to call once per channel."""
    global ALLOWED_CHAT_ID
    if not chat_id:
        return
    ALLOWED_CHAT_IDS.clear()
    ALLOWED_CHAT_IDS.add(str(chat_id))
    ALLOWED_CHAT_ID = str(chat_id)


def is_authorized(ctx: Any) -> bool:
    if ctx.test_mode:
        return True
    return str(ctx.chat_id) in ALLOWED_CHAT_IDS
