"""
====================================

Logger Module

====================================
"""

import atexit
import logging
import os

LOG_DIR = "logs"

os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "zetbot.log")

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"

if not logging.getLogger().handlers:
    # Only construct the handlers when basicConfig will actually install them.
    # Otherwise the FileHandler below would be created, dropped, and garbage
    # collected while still open — emitting a ResourceWarning for zetbot.log.
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )
else:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)


def _close_handlers() -> None:
    """Close root handlers at interpreter shutdown.

    Prevents a ResourceWarning for the unclosed zetbot.log file handler and
    avoids leaking the file descriptor when the process exits.
    """
    for handler in logging.getLogger().handlers:
        try:
            handler.close()
        except Exception:
            pass


atexit.register(_close_handlers)

logger = logging.getLogger("ZetBot")
