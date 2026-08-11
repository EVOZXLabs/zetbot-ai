"""
Repair script for ZetBot AI — backfills missing fields on old order
records inside data/paper_state.json so Order(**o) stops crashing
on startup.

Safe to run multiple times (idempotent). Makes a timestamped backup
of paper_state.json before writing anything.

Usage (from the project root, with venv active):
    python repair_paper_orders.py
"""

import json
import os
import shutil
from datetime import datetime, timezone

STATE_PATH = "data/paper_state.json"

# All fields the Order dataclass requires, with sane fallback defaults.
REQUIRED_DEFAULTS = {
    "id": "unknown",
    "symbol": "",
    "side": "SELL",
    "type": "MARKET",
    "quantity": 0.0,
    "filled_quantity": 0.0,
    "entry_price": 0.0,
    "fill_price": 0.0,
    "slippage": 0.0,
    "entry_fee": 0.0,
    "exit_price": 0.0,
    "exit_fee": 0.0,
    "total_cost": 0.0,
    "total_proceeds": 0.0,
    "net_pnl": 0.0,
    "net_pnl_pct": 0.0,
    "status": "CLOSED",
    "created_at": "",
    "filled_at": "",
    "closed_at": "",
    "exit_reason": "",
}


def main() -> None:
    if not os.path.exists(STATE_PATH):
        print(f"  Not found: {STATE_PATH} — nothing to repair.")
        return

    with open(STATE_PATH) as f:
        state = json.load(f)

    orders = state.get("orders", [])
    if not orders:
        print("  No orders found in paper_state.json — nothing to repair.")
        return

    repaired = 0
    for o in orders:
        missing = [k for k in REQUIRED_DEFAULTS if k not in o]
        if not missing:
            continue

        # Best-effort: derive a couple of fields intelligently instead of
        # just zero-filling, when we have enough info to do so.
        if "exit_price" in missing and "fill_price" in o:
            o["exit_price"] = o["fill_price"]
        if "entry_price" in missing and "fill_price" in o:
            # Best guess when nothing else is available.
            o["entry_price"] = o.get("fill_price", 0.0)
        if "total_cost" in missing:
            entry_price = o.get("entry_price", REQUIRED_DEFAULTS["entry_price"])
            qty = o.get("quantity", REQUIRED_DEFAULTS["quantity"])
            o["total_cost"] = round(float(entry_price) * float(qty), 2)
        if "net_pnl_pct" in missing:
            net_pnl = o.get("net_pnl", 0.0) or 0.0
            cost = o.get("total_cost", 0.0) or 0.0
            o["net_pnl_pct"] = round((net_pnl / cost * 100), 2) if cost else 0.0

        for key in missing:
            if key not in o:
                o[key] = REQUIRED_DEFAULTS[key]

        repaired += 1

    if repaired == 0:
        print("  All orders already complete — nothing to repair.")
        return

    # Backup before writing.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{STATE_PATH}.bak.{ts}"
    shutil.copy2(STATE_PATH, backup_path)
    print(f"  Backup saved: {backup_path}")

    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)

    print(f"  Repaired {repaired} order record(s) in {STATE_PATH}.")


if __name__ == "__main__":
    main()
