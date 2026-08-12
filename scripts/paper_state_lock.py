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

import csv  # noqa: E402

# Canonical columns for the CLOSED-TRADES store (single source of truth
# for /history, /summary and all derived accounting).  Must stay in sync
# with ``PaperExport.trade_history_csv`` in ``scripts/paper_trading_engine``.
_TRADE_HISTORY_COLUMNS = [
    "id", "symbol", "side",
    "quantity", "entry_price", "fill_price",
    "exit_price", "entry_fee", "exit_fee",
    "net_pnl", "net_pnl_pct",
    "created_at", "filled_at", "closed_at",
]


def rebuild_trade_history_csv(data_dir: str = "data") -> int:
    """Rebuild ``paper_trade_history.csv`` from the authoritative ledgers.

    CLOSED TRADES have exactly ONE source of truth: ``paper_trade_history.csv``.
    It is derived from the authoritative ledgers — ``paper_state.json`` (the
    wallet ledger) and ``paper_orders.json`` (the order ledger) — so it can
    never drift from the engine's view of what actually closed.  This is the
    repair path used by:

      * a manual /sell closure (``order_manager._close_paper_position_on_sell``),
      * the pipeline persist step (``pipeline._persist_paper_state``),
      * startup reconciliation (``accounting_reconcile.reconcile``).

    CLOSED SELL orders are merged from BOTH ledgers (deduped by order id,
    ``paper_state.json`` winning) because different writers append SELL
    records to only one of the two files.  A rebuild that ignored
    ``paper_orders.json`` would silently erase real closed trades whenever
    ``paper_state.json`` still held stale/legacy state — exactly the
    corruption this repair path exists to prevent.

    Returns the number of closed trades written (0 if none / no state).
    """
    csv_path = os.path.join(data_dir, "paper_trade_history.csv")

    def _load(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    state = _load(os.path.join(data_dir, "paper_state.json"))
    order_state = _load(os.path.join(data_dir, "paper_orders.json"))

    state_orders = (state.get("orders") if isinstance(state, dict) else None) or []
    order_orders = (order_state.get("orders") if isinstance(order_state, dict) else None) or []
    positions = (state.get("positions") if isinstance(state, dict) else None) or {}

    # Merge CLOSED SELL orders from both ledgers, deduped by order id
    # (paper_state.json is authoritative when both have the same order).
    orders_by_id: dict[str, dict] = {}
    for o in order_orders:
        if o.get("status") == "CLOSED" and o.get("side") == "SELL":
            orders_by_id[o.get("id", "") or f"ord-{o.get('symbol', '')}"] = o
    for o in state_orders:
        if o.get("status") == "CLOSED" and o.get("side") == "SELL":
            orders_by_id[o.get("id", "") or f"ord-{o.get('symbol', '')}"] = o
    orders = list(orders_by_id.values())

    trades: list[dict[str, Any]] = []
    covered: set[str] = set()

    # 1) CLOSED SELL orders are the primary representation of a completed
    #    trade (they already carry entry/exit/net_pnl from the close path).
    for o in orders:
        sym = o.get("symbol", "")
        covered.add(sym)
        trades.append({k: o.get(k, "") for k in _TRADE_HISTORY_COLUMNS})

    # 2) CLOSED positions that (for any reason) have no SELL order yet still
    #    represent a realized trade — derive one so accounting stays honest.
    for sym, p in positions.items():
        if sym in covered:
            continue
        if not isinstance(p, dict) or p.get("status") != "CLOSED":
            continue
        exit_px = p.get("current_price") or p.get("exit_price") or p.get("entry_price", 0)
        trades.append({
            "id": f"pos-{sym}",
            "symbol": sym,
            "side": "SELL",
            "quantity": p.get("remaining_qty", p.get("quantity", 0)),
            "entry_price": p.get("entry_price", 0),
            "fill_price": p.get("entry_price", 0),
            "exit_price": exit_px,
            "entry_fee": 0.0,
            "exit_fee": 0.0,
            "net_pnl": p.get("realized_pnl", p.get("total_pnl", 0)) or 0.0,
            "net_pnl_pct": p.get("total_pnl_pct", 0) or 0.0,
            "created_at": p.get("opened_at", p.get("entry_time", "")),
            "filled_at": p.get("opened_at", p.get("entry_time", "")),
            "closed_at": p.get("closed_at", ""),
        })

    try:
        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_TRADE_HISTORY_COLUMNS)
            w.writeheader()
            for t in trades:
                w.writerow(t)
    except OSError:
        return 0
    return len(trades)


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


def _atomic_write_lines(path: str, lines: list[str]) -> None:
    """Atomically write plain-text *lines* (one per line) to *path*.

    Same temp-file + ``os.replace`` pattern as ``atomic_write_json`` so a
    concurrent reader never sees a partially-written file.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, suffix=".tmp", prefix=".atomic_",
    )
    try:
        with os.fdopen(fd, "w") as f:
            for line in lines:
                f.write(f"{line}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def add_notified_buy(symbol: str, notified_path: str = "data/.notified_buys") -> None:
    """Atomically record *symbol* in the BUY_OPENED dedup file.

    The file is line-per-symbol plain text. The read-modify-write runs
    under ``PAPER_STATE_LOCK`` and the write is atomic (temp file +
    ``os.replace``), so a concurrent writer (the paper-engine startup
    daemon thread vs. the pipeline scheduler — BUG-4) can never clobber a
    symbol added by the other thread, and a concurrent reader never sees a
    half-written dedup file.
    """
    with PAPER_STATE_LOCK:
        notified: set[str] = set()
        try:
            with open(notified_path) as nf:
                notified = set(line.strip() for line in nf if line.strip())
        except (FileNotFoundError, OSError):
            notified = set()
        notified.add(symbol)
        _atomic_write_lines(notified_path, sorted(notified))


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
        data["total_positions"] = len(merged)
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


def sync_positions_from_state(
    state_path: str = "data/paper_state.json",
    positions_path: str = "data/positions.json",
) -> bool:
    """Strictly reconcile ``positions.json`` against ``paper_state.json``.

    ``paper_state.json`` is the authoritative record of every position the
    paper engine knows about (OPEN and CLOSED).  Every ``positions.json``
    writer (engine ``_save_state``, pipeline write-ahead, monitor, exit
    gate, order manager) MERGES by symbol and never removes, so a record
    whose symbol no longer exists in ``paper_state.json`` — a legacy or
    test leftover, or the survivor of a partial state reset — stays
    visible to Telegram ``/positions`` forever (ghost position).

    This function is the ONE removal path: under ``PAPER_STATE_LOCK`` it
    keeps only symbols present in ``paper_state.json``, adds any engine
    positions that are missing from ``positions.json``, and recomputes
    ``total_positions`` / ``active_count`` / ``closed_count`` so the
    counters always match the list.

    Returns True when the file was rewritten, False when nothing changed.
    """
    from scripts.position_status import is_open, OPEN_STATUSES  # noqa: PLC0415

    with PAPER_STATE_LOCK:
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}

        state_positions = state.get("positions") or {}

        try:
            with open(positions_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {"positions": []}

        merged: dict[str, dict[str, Any]] = {}
        for pos in data.get("positions", []):
            sym = pos.get("symbol")
            if sym:
                merged[sym] = pos

        # Drop ghosts: symbols in positions.json that paper_state.json
        # does not know about.
        dropped = [sym for sym in merged if sym not in state_positions]
        for sym in dropped:
            merged.pop(sym)

        # Add engine positions missing from positions.json so the file
        # mirrors paper_state.json exactly.
        added: list[str] = []
        for sym, vp in state_positions.items():
            if sym not in merged:
                merged[sym] = dict(vp)
                added.append(sym)

        if not dropped and not added:
            # Verify counters while we are here — keep the file consistent
            # even when the symbol set already matches.
            positions = list(merged.values())
            expected = {
                "total_positions": len(positions),
                "active_count": sum(1 for p in positions if is_open(p.get("status"))),
                "closed_count": sum(
                    1 for p in positions if p.get("status") not in OPEN_STATUSES
                ),
            }
            if all(data.get(k) == v for k, v in expected.items()):
                return False

        positions = list(merged.values())
        data["positions"] = positions
        data["total_positions"] = len(positions)
        data["active_count"] = sum(1 for p in positions if is_open(p.get("status")))
        data["closed_count"] = sum(
            1 for p in positions if p.get("status") not in OPEN_STATUSES
        )
        try:
            os.makedirs(os.path.dirname(positions_path) or ".", exist_ok=True)
            atomic_write_json(positions_path, data, indent=2, default=str)
        except OSError:
            return False
        return True


def prune_closed_positions(
    max_closed: int = 50,
    positions_path: str = "data/positions.json",
    archive_path: str = "data/positions_archive.json",
) -> int:
    """Move old CLOSED positions to an archive file to keep positions.json lean.

    Keeps the most recent ``max_closed`` closed positions in
    ``positions.json``.  Any excess (oldest by ``opened_at``) are moved
    to ``positions_archive.json`` so trade history is never lost.

    Returns the number of entries archived.
    """
    from scripts.position_status import is_open  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    with PAPER_STATE_LOCK:
        try:
            with open(positions_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return 0

        positions = data.get("positions", [])
        open_pos = [p for p in positions if is_open(p.get("status"))]
        closed_pos = [p for p in positions if not is_open(p.get("status"))]

        if len(closed_pos) <= max_closed:
            return 0  # nothing to prune

        # Sort closed by opened_at so oldest are pruned first
        closed_pos.sort(key=lambda p: p.get("opened_at", ""), reverse=False)
        to_archive = closed_pos[: len(closed_pos) - max_closed]
        to_keep = closed_pos[len(closed_pos) - max_closed :]

        # Append to archive
        try:
            with open(archive_path) as f:
                archive = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            archive = {"positions": []}

        archive.setdefault("positions", []).extend(to_archive)
        if "archived_runs" not in archive:
            archive["archived_runs"] = []
        archive["archived_runs"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": len(to_archive),
        })

        try:
            os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
            atomic_write_json(archive_path, archive, indent=2, default=str)
        except OSError:
            pass

        # Update positions.json with pruned list
        data["positions"] = open_pos + to_keep
        data["total_positions"] = len(data["positions"])
        data["active_count"] = len(open_pos)
        data["closed_count"] = len(to_keep)
        try:
            atomic_write_json(positions_path, data, indent=2, default=str)
        except OSError:
            return 0

        return len(to_archive)
