"""Shared lock that serializes PAPER-state file writes across threads (BUG-4).

In PAPER mode two independent threads drive trading state:

    * the position monitor (``main.py``, ~every 60 s) — reconciles open
      positions and, on closure, writes ``data/positions.json`` /
      ``data/paper_balance.json`` / ``data/paper_orders.json`` /
      ``data/paper_state.json`` (``_update_paper_on_closure``), and
    * the pipeline scheduler (~every 300 s) — executes plans and persists
      paper state through the paper engine (``_save_state``),
      ``order_manager._sync_paper_files`` /
      ``_close_paper_position_on_sell``, ``pipeline._persist_paper_state``
      and ``PaperExecutionProvider._save_positions`` / ``PaperBalance.save``.

Both threads read-modify-write the SAME JSON files. Without a shared lock
they can interleave (two simultaneous ``open(..., "w")`` corrupt a file) or
silently lose each other's updates (a stale reader overwrites a fresh
writer) — the paper analog of the BUG-2 LIVE oversell race.

This module provides ONE re-entrant lock that every paper-state file writer
acquires around its read-modify-write, plus ``merge_positions`` for the two
bulk ``positions.json`` writers (the monitor and the pipeline) so a stale
full-file overwrite can never clobber a symbol a concurrent writer just
updated.

Deliberately scoped to FILE read-modify-write sections only — never held
across network I/O or order execution, so it cannot serialize trading
itself. LIVE mode does not use this lock: its exits are serialized per
symbol by ``scripts.exit_gate`` and it never writes paper accounting
(BUG-3).
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
import threading
from typing import Any, Callable, TypeVar, cast

PAPER_STATE_LOCK = threading.RLock()


# -----------------------------------------------------------------------
#  Atomic JSON writer (BUG B: prevents partial-read by Telegram commands)
# -----------------------------------------------------------------------

def atomic_write_json(path: str, data: Any, **kwargs: Any) -> None:
    """Atomically write *data* as JSON to *path* via temp-file + ``os.replace``.

    On POSIX (Linux), ``os.replace`` is atomic at the filesystem level so
    concurrent readers (Telegram command threads) see either the complete
    **old** file or the complete **new** file — never a truncated/
    partially-written version that causes ``json.JSONDecodeError``.

    A temp file in the same directory is written first (with ``fsync``),
    then atomically renamed on top of the target path.  The temp file is
    cleaned up on failure.

    Extra keyword arguments (e.g. ``indent=2``, ``default=str``) are
    forwarded to ``json.dump``.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, suffix=".json.tmp", prefix=".atomic_",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, **kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

F = TypeVar("F", bound=Callable[..., Any])


def paper_state_writes(fn: F) -> F:
    """Serialize one paper-state file writer against every other writer.

    Applies to functions/methods whose body read-modify-writes the shared
    paper accounting files. Re-entrant, so nested writers in the same
    thread (e.g. ``_update_paper_on_closure`` -> ``_sync_paper_state_on_closure``)
    are safe.
    """

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with PAPER_STATE_LOCK:
            return fn(*args, **kwargs)

    return cast(F, _wrapped)


def merge_positions(positions: list[dict[str, Any]]) -> None:
    """Atomically merge ``positions`` into ``data/positions.json`` by symbol.

    Re-reads the file under ``PAPER_STATE_LOCK`` so updates a concurrent
    writer persisted in the meantime (e.g. the pipeline's new BUY or the
    monitor's closure) are preserved instead of being clobbered by a stale
    full-file overwrite. Recomputed ``active_count`` / ``closed_count``.
    """
    from scripts.exit_gate import POSITIONS_PATH  # noqa: PLC0415
    from scripts.position_status import is_open, OPEN_STATUSES  # noqa: PLC0415

    with PAPER_STATE_LOCK:
        try:
            with open(POSITIONS_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {"positions": []}
        by_symbol: dict[str, dict[str, Any]] = {}
        for pos in data.get("positions", []):
            if pos.get("symbol"):
                by_symbol[pos["symbol"]] = pos
        for pos in positions:
            if pos.get("symbol"):
                by_symbol[pos["symbol"]] = pos
        merged = list(by_symbol.values())
        data["positions"] = merged
        data["active_count"] = sum(
            1 for p in merged if is_open(p.get("status"))
        )
        data["closed_count"] = sum(
            1 for p in merged if p.get("status") not in OPEN_STATUSES
        )
        try:
            os.makedirs(os.path.dirname(POSITIONS_PATH) or ".", exist_ok=True)
            atomic_write_json(POSITIONS_PATH, data, indent=2, default=str)
        except OSError:
            pass
