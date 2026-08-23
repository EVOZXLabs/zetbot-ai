"""Per-symbol serialization for LIVE exit paths (BUG-2 fix).

In LIVE mode several independent threads drive TP/SL exits:

    * ``main.py``'s position monitor (~every 60 s)
    * the pipeline reconciliation stage (~every 300 s)
    * the protection-order scheduler (~every 8 s)
    * Telegram ``/sell`` (on demand)

Each of them used to read ``data/positions.json`` independently, decide
"TP/SL hit", and submit a MARKET sell. Two concurrent readers could
both sell the same quantity — an oversell (see the BUG-2 audit and
``tests/test_exit_gate_concurrency.py``).

This module is the ONE shared synchronization mechanism every exit path
uses:

    * ``exit_guard(symbol)`` — a per-symbol re-entrant lock, so the
      read -> decide -> sell -> persist cycle for a symbol is serialized
      across ALL exit threads (not just one caller's own instance), and
    * ``load_position`` / ``save_position`` — atomic per-symbol
      ``positions.json`` read-modify-write, so every decision is made
      against the freshest authoritative state instead of a stale copy
      that a concurrent thread may already have acted on.

``reconcile_exit()`` wraps the whole TP/SL reconcile cycle for one
symbol and is used by both the monitor and the pipeline in LIVE mode.
``ProtectionManager`` and ``OrderManager`` guard on the same per-symbol
lock so protection reconciliation and manual sells serialize against
automatic exits too. Protection orders are cancelled BEFORE the market
sell (``reconcile_exit``), never after.

PAPER mode is deliberately NOT routed through this module — the paper
reconciliation runs in a single scheduler thread and must keep its
existing behavior.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Optional

from scripts.position_status import OPEN_STATUSES

POSITIONS_PATH = "data/positions.json"

_log = logging.getLogger("ZetBot")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(symbol: str) -> threading.RLock:
    """Return the shared per-symbol re-entrant lock.

    One lock instance per symbol, shared by every exit path, so the
    lock identity — not any per-caller instance — is what serializes.
    """
    key = symbol or "_GLOBAL"
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


@contextmanager
def exit_guard(symbol: str) -> Any:
    """Serialize one symbol's exit work across all live exit paths."""
    with lock_for(symbol):
        yield


def load_position(symbol: str) -> Optional[dict[str, Any]]:
    """Return the authoritative current position for ``symbol`` (or None)."""
    if not symbol:
        return None
    try:
        with open(POSITIONS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    for pos in data.get("positions", []):
        if pos.get("symbol") == symbol:
            return dict(pos)
    return None


def save_position(symbol: str, position: dict[str, Any]) -> None:
    """Replace one symbol's record in ``positions.json`` (others preserved).

    Re-reads the file so an update for another symbol written between our
    read and this write is not clobbered. Callers must hold
    ``exit_guard(symbol)``.
    """
    if not symbol:
        return
    try:
        os.makedirs(os.path.dirname(POSITIONS_PATH) or ".", exist_ok=True)
        with open(POSITIONS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"positions": []}
    positions = data.get("positions", [])
    replaced = False
    for i, pos in enumerate(positions):
        if pos.get("symbol") == symbol:
            positions[i] = position
            replaced = True
            break
    if not replaced:
        positions.append(position)
    data["positions"] = positions
    data["total_positions"] = len(positions)
    data["active_count"] = sum(
        1 for p in positions if p.get("status") in OPEN_STATUSES
    )
    data["closed_count"] = sum(
        1 for p in positions if p.get("status") not in OPEN_STATUSES
    )
    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(POSITIONS_PATH, data, indent=2, default=str)
    except OSError:
        pass


def exit_triggered(position: dict[str, Any], current_price: float) -> bool:
    """Whether ``ExecutionPipeline.reconcile_position`` will submit an exit
    sell for this position at ``current_price``.

    Mirrors the trigger rules in ``scripts/execution_pipeline.py`` so a
    caller can cancel resting protection orders BEFORE the market sell —
    a stale protection leg (a full-quantity limit order) would otherwise
    double-fill together with the market sell.
    """
    if not current_price or current_price <= 0:
        return False
    qty = float(position.get("quantity", 0) or 0)
    remaining = float(position.get("remaining_qty", qty) or 0)
    if remaining <= 0:
        return False
    for tp_key, fraction in (("tp1", 0.30), ("tp2", 0.30), ("tp3", 0.40)):
        tp_price = float(position.get(tp_key, 0) or 0)
        if tp_price <= 0:
            continue
        if position.get(f"{tp_key}_hit", False):
            continue
        if current_price >= tp_price and qty * fraction > 0:
            return True
    stop_loss = float(position.get("current_stop", 0) or position.get("stop_loss", 0) or 0)
    if stop_loss > 0 and current_price <= stop_loss:
        return True
    return False


def reconcile_exit(
    pipeline: Any,
    symbol: str,
    current_price: float,
    plan: Optional[dict[str, Any]] = None,
    *,
    cancel_protection: Optional[Callable[[str], None]] = None,
    on_reconciled: Optional[
        Callable[[dict[str, Any], Optional[dict[str, Any]]], None]
    ] = None,
) -> Optional[dict[str, Any]]:
    """Thread-safe per-symbol TP/SL reconcile (LIVE mode).

    Under the per-symbol lock:

        1. re-read the authoritative position state (not the caller's
           possibly-stale copy),
        2. when an exit is about to fire, cancel resting protection
           orders FIRST so a protection leg can't double-fill,
        3. run the shared reconcile (submits the market sell if a level
           is hit),
        4. persist the result, then run ``on_reconciled(pos, result)``
           still inside the lock so follow-up bookkeeping (e.g.
           ``closure_notified``) can be saved atomically.
    """
    with exit_guard(symbol):
        pos = load_position(symbol)
        if pos is None or pos.get("status") not in OPEN_STATUSES:
            return pos
        # Defense-in-depth: a position whose entry price is unknown (never
        # reconstructed from exchange fill history — legacy/manual/dust
        # balance) must NOT be acted on by any exit path. The same rule
        # guards protective orders via ``require_entry_price()`` in
        # ``ProtectionManager._guard``; a TP/SL decision here would have
        # nothing real to base stop/target levels on.
        if pos.get("entry_price") is None:
            _log.warning(
                "reconcile_exit: skipping %s — entry price unknown "
                "(exchange balance, not bot-managed); no SL/TP exit.",
                symbol,
            )
            return pos
        if cancel_protection is not None and exit_triggered(pos, current_price):
            try:
                cancel_protection(symbol)
            except Exception:
                pass
        reconciled = pipeline.reconcile_position(
            symbol, current_price, pos, plan=plan or {},
        )
        if reconciled is not None:
            save_position(symbol, reconciled)
        if on_reconciled is not None:
            try:
                on_reconciled(pos, reconciled)
            except Exception:
                pass
        return reconciled
