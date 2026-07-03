"""
====================================

Logger Module

====================================
"""

import logging
import os

LOG_DIR = "logs"

os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "zetbot.log")

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s | %(levelname)s | %(message)s",

    handlers=[

        logging.FileHandler(LOG_FILE),

        logging.StreamHandler()

    ]

)

logger = logging.getLogger("ZetBot")
