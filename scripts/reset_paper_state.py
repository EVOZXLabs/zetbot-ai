"""Safely reset all paper trading state files.

Removes:
    data/paper_balance.json
    data/paper_orders.json
    data/paper_orders.csv
    data/paper_trade_history.csv
    data/paper_state.json
    data/positions.json

Does NOT delete source code or configuration files.

Usage::

    python scripts/reset_paper_state.py
"""

import json
import os
import sys
from datetime import datetime, timezone

DATA_DIR = "data"

FILES_TO_REMOVE = [
    "paper_balance.json",
    "paper_orders.json",
    "paper_orders.csv",
    "paper_trade_history.csv",
    "paper_state.json",
    "positions.json",
]


def reset_paper_state(data_dir: str = DATA_DIR) -> list[str]:
    """Remove paper trading state files. Returns list of removed files."""
    removed: list[str] = []
    for fname in FILES_TO_REMOVE:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def main() -> None:
    removed = reset_paper_state()
    if removed:
        print(f"Removed {len(removed)} file(s):")
        for f in removed:
            print(f"  {f}")
    else:
        print("No paper state files found to remove.")


if __name__ == "__main__":
    main()
