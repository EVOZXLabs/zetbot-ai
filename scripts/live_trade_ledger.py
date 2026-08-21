"""Live spot trade-history ledger — actual-exchange-fill accounting (LIVE).

PAPER trades are accounted by ``paper_trade_history.csv`` (see
``scripts/paper_state_lock`` / ``scripts/metrics_manager``). LIVE trades get
their own append-only JSONL ledger — ``data/live_trade_history.jsonl`` — built
from the ACTUAL exchange fills, so ``/history``, ``/summary``, ``/performance``
and the LIVE account snapshot report real money: never paper data and never
price estimates.

One ledger record per fully-closed LIVE position (schema: trade_id, symbol,
quantity, entry_price, exit_price, entry_fee, exit_fee, gross_pnl, net_pnl,
net_pnl_pct, opened_at, closed_at, holding_duration, exit_reason,
buy_order_id, sell_order_id).

A position may close over several sell fills (tp1/tp2/tp3/sl, or repeated
manual ``/sell`` legs). The actual buy fill is snapshotted at entry
(``record_live_entry``); every actual sell fill is accumulated
(``record_live_exit_fill``) in a pending-closure record
(``data/live_pending_closures.json``) and finalized ONCE when the position is
flat. A crash mid-way therefore can never produce a duplicate or half-written
trade: the pending record survives restart and the finalize step re-checks the
ledger by ``(buy_order_id, sell_order_id)`` before appending.

Concurrency: every writer already serializes per-symbol via
``scripts.exit_gate`` (automatic TP/SL exits in ``ExecutionPipeline`` run
inside ``reconcile_exit``; manual orders in ``OrderManager`` hold the same
per-symbol lock), so the pending/ledger read-modify-write below is
single-writer per symbol. The ledger append itself is append-only
(``os.O_APPEND``) with ``fsync``.

Safety: every function here is best-effort and never raises — a ledger write
failure must never interrupt trading (mirrors ``emit_event`` in
``scripts/execution_provider``).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

LIVE_TRADE_HISTORY_PATH = "data/live_trade_history.jsonl"
LIVE_PENDING_CLOSURES_PATH = "data/live_pending_closures.json"

# Tolerance on covered quantity before a position counts as "flat". Fees
# paid in the base asset / exchange rounding can leave the sellable amount
# a hair below the bought amount; 0.5 % keeps those closes finalizable
# while never finalizing a genuinely half-sold position.
_CLOSE_COVERAGE = 0.97

# A pending closure whose last activity is older than this is treated as
# abandoned (e.g. a position closed on the exchange itself, outside the
# bot's tracked fills) and is replaced by a fresh entry on the next buy.
_PENDING_STALE_DAYS = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_gap_seconds(a: str, b: str) -> float:
    """Seconds from ISO timestamp *a* to *b*, or 0.0 when unparseable."""
    try:
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        db = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return round((db - da).total_seconds(), 2)
    except (ValueError, TypeError):
        return 0.0


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Atomic temp-file + ``os.replace`` JSON write (no paper lock used —
    the live ledger must stay fully independent of paper accounting)."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, suffix=".json.tmp", prefix=".atomic_",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _load_pending() -> dict[str, Any]:
    try:
        with open(LIVE_PENDING_CLOSURES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_pending(pending: dict[str, Any]) -> None:
    try:
        _atomic_write_json(LIVE_PENDING_CLOSURES_PATH, pending)
    except Exception:
        pass


def _ledger_keys() -> set[tuple[str, str]]:
    """Every ``(buy_order_id, sell_order_id)`` already in the ledger."""
    keys: set[tuple[str, str]] = set()
    try:
        with open(LIVE_TRADE_HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                buy = str(rec.get("buy_order_id", "") or "")
                sell = str(rec.get("sell_order_id", "") or "")
                if buy and sell:
                    keys.add((buy, sell))
    except (FileNotFoundError, OSError):
        pass
    return keys


def _pending_stale(rec: dict[str, Any]) -> bool:
    """True when a pending closure has had no fills for a long time."""
    fills = rec.get("sell_fills") or []
    if not fills:
        opened = rec.get("opened_at", "") or ""
        if not opened:
            return False
        try:
            opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        age = datetime.now(timezone.utc) - opened_dt.replace(tzinfo=timezone.utc)
        return age.total_seconds() > _PENDING_STALE_DAYS * 86400
    last = fills[-1].get("timestamp", "") or ""
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    age = datetime.now(timezone.utc) - last_dt.replace(tzinfo=timezone.utc)
    return age.total_seconds() > _PENDING_STALE_DAYS * 86400


def record_live_entry(result: Any, symbol: str = "") -> None:
    """Persist the ACTUAL buy fill as the pending-closure entry snapshot.

    Called right after a FILLED LIVE BUY (automatic pipeline via
    ``ExecutionPipeline.execute_plan`` or manual ``/buy`` via
    ``OrderManager``). ``result`` is an OrderResult carrying the
    exchange-confirmed fill (order_id, filled_price, filled_amount, fee,
    timestamp). Best-effort: never raises.
    """
    try:
        sym = result.symbol if hasattr(result, "symbol") else symbol
        if not sym:
            return
        qty = float(getattr(result, "filled_amount", 0) or 0)
        price = float(getattr(result, "filled_price", 0) or 0)
        if qty <= 0 or price <= 0:
            return
        fee = float(getattr(result, "fee", 0) or 0)
        order_id = str(getattr(result, "order_id", "") or "")
        ts = getattr(result, "timestamp", "") or _now()

        pending = _load_pending()
        existing = pending.get(sym)
        # One-position rule (AGENTS.md): a fresh BUY while an entry snapshot
        # with NO recorded sells means the previous trade never closed — it
        # is replaced. A mid-close record (fills already recorded) is kept.
        if existing is not None and existing.get("sell_fills") and not _pending_stale(existing):
            return

        pending[sym] = {
            "symbol": sym,
            "trade_id": f"lt_{order_id}" if order_id else f"lt_{sym}_{ts}",
            "buy_order_id": order_id,
            "quantity": qty,
            "entry_price": price,
            "entry_fee": fee,
            "entry_cost": round(price * qty, 8),
            "opened_at": ts,
            "entry_source": "actual_fill",
            "sell_fills": [],
        }
        _save_pending(pending)
    except Exception:
        pass


def record_live_exit_fill(
    result: Any,
    reason: str = "",
    *,
    entry_fallback: Optional[dict[str, Any]] = None,
    symbol: str = "",
) -> None:
    """Accumulate one ACTUAL sell fill and finalize when the position is flat.

    Called after every FILLED LIVE SELL (automatic TP/SL leg via
    ``ExecutionPipeline.reconcile_position`` or manual ``/sell`` via
    ``OrderManager``). ``reason`` uses the exit vocabulary ("Take Profit" /
    "Stop Loss" / "Manual Close"). ``entry_fallback`` (the position dict
    from ``positions.json``) is used ONLY when no buy-fill snapshot exists —
    e.g. a position opened before this ledger existed — and is never used to
    estimate fills. Best-effort: never raises.
    """
    try:
        sym = result.symbol if hasattr(result, "symbol") else symbol
        if not sym:
            return
        qty = float(getattr(result, "filled_amount", 0) or 0)
        price = float(getattr(result, "filled_price", 0) or 0)
        if qty <= 0 or price <= 0:
            return
        fee = float(getattr(result, "fee", 0) or 0)
        order_id = str(getattr(result, "order_id", "") or "")
        ts = getattr(result, "timestamp", "") or _now()

        pending = _load_pending()
        rec = pending.get(sym)

        if rec is None or not rec.get("buy_order_id"):
            if rec is None and entry_fallback is not None:
                rec = _pending_from_fallback(sym, entry_fallback)
            if rec is None:
                # No entry data at all — we refuse to estimate (see the
                # module docstring); the position simply is not recorded.
                return
            pending[sym] = rec

        rec.setdefault("sell_fills", []).append({
            "order_id": order_id,
            "price": round(price, 8),
            "qty": round(qty, 8),
            "fee": round(fee, 8),
            "timestamp": ts,
            "reason": reason or "Exit",
        })
        rec["sell_fills"].sort(key=lambda f: f.get("timestamp", ""))
        _save_pending(pending)

        if _fills_cover(rec):
            _finalize(sym, rec)
    except Exception:
        pass


def _pending_from_fallback(
    symbol: str, position: dict[str, Any],
) -> Optional[dict[str, Any]]:
    entry = float(position.get("entry_price", 0) or 0)
    qty = float(
        position.get("quantity", 0) or position.get("remaining_qty", 0) or 0
    )
    if entry <= 0 or qty <= 0:
        return None
    opened_at = position.get("opened_at") or position.get("entry_time", "") or ""
    return {
        "symbol": symbol,
        "trade_id": f"lt_{symbol}_{opened_at or _now()}",
        "buy_order_id": "",
        "quantity": qty,
        "entry_price": entry,
        "entry_fee": 0.0,
        "entry_cost": round(entry * qty, 8),
        "opened_at": opened_at,
        "entry_source": "positions_json_fallback",
        "sell_fills": [],
    }


def _fills_cover(rec: dict[str, Any]) -> bool:
    entry_qty = float(rec.get("quantity", 0) or 0)
    if entry_qty <= 0:
        return False
    filled = sum(float(f.get("qty", 0) or 0) for f in rec.get("sell_fills", []))
    return filled >= entry_qty * _CLOSE_COVERAGE


def _best_exit_reason(fills: list[dict[str, Any]], net_pnl: float) -> str:
    """Determine the most representative exit reason from a set of fills.

    ``_finalize`` previously used the *last* fill's reason, which is
    misleading when partial TPs are followed by a SL (or vice versa):
    GPS/IDR had 3 TP fills but lost money → reason showed "Take Profit";
    PIPPIN/IDR's last fill was SL but was overall profitable.
    """
    if not fills:
        return "Exit"
    reasons = {}
    for f in fills:
        reason = str(f.get("reason") or "Exit")
        qty = float(f.get("qty", 0) or 0)
        reasons[reason] = reasons.get(reason, 0.0) + qty
    dominant = max(reasons, key=reasons.get)
    other_reasons = [r for r in reasons if r != dominant]
    if other_reasons:
        return "TP/SL"
    return dominant


def _finalize(symbol: str, rec: dict[str, Any]) -> None:
    """Append the closed-trade record once (deduped by buy/sell order ids)."""
    fills = [f for f in (rec.get("sell_fills") or []) if float(f.get("qty", 0) or 0) > 0]
    if not fills:
        return
    entry_qty = float(rec.get("quantity", 0) or 0)
    if entry_qty <= 0:
        return

    # Greedily keep fills (oldest first) until the entry quantity is covered,
    # clamping the last one — guards against extra externally-managed quantity
    # that would otherwise overstate PnL.
    kept: list[dict[str, Any]] = []
    covered = 0.0
    for f in sorted(fills, key=lambda x: x.get("timestamp", "")):
        if covered >= entry_qty:
            break
        take = min(float(f.get("qty", 0) or 0), entry_qty - covered)
        if take <= 0:
            continue
        kept.append({**f, "qty": take})
        covered += take
    if not kept:
        return

    exit_qty = sum(float(f["qty"]) for f in kept)
    exit_price = (
        sum(float(f["qty"]) * float(f["price"]) for f in kept) / exit_qty
        if exit_qty > 0 else 0.0
    )
    exit_fee = sum(float(f.get("fee", 0) or 0) for f in kept)

    buy_order_id = str(rec.get("buy_order_id", "") or "")
    sell_order_id = str(kept[-1].get("order_id", "") or "")

    # Dedup: a restart or a reconciliation re-run must never double-record.
    if buy_order_id and sell_order_id and (buy_order_id, sell_order_id) in _ledger_keys():
        _clear_pending(symbol)
        return

    entry_price = float(rec.get("entry_price", 0) or 0)
    entry_fee = float(rec.get("entry_fee", 0) or 0)
    gross_pnl = exit_qty * exit_price - entry_qty * entry_price
    net_pnl = gross_pnl - entry_fee - exit_fee
    net_pnl_pct = (
        round(net_pnl / (entry_qty * entry_price) * 100.0, 2)
        if entry_qty * entry_price > 0 else 0.0
    )

    opened_at = str(rec.get("opened_at", "") or "")
    closed_at = str(kept[-1].get("timestamp", "") or _now())

    record = {
        "trade_id": str(
            rec.get("trade_id") or f"lt_{buy_order_id or sell_order_id}"
        ),
        "symbol": symbol,
        "quantity": round(entry_qty, 8),
        "entry_price": round(entry_price, 8),
        "exit_price": round(exit_price, 8),
        "entry_fee": round(entry_fee, 8),
        "exit_fee": round(exit_fee, 8),
        "gross_pnl": round(gross_pnl, 8),
        "net_pnl": round(net_pnl, 8),
        "net_pnl_pct": net_pnl_pct,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "holding_duration": _iso_gap_seconds(opened_at, closed_at),
        "exit_reason": _best_exit_reason(kept, net_pnl),
        "buy_order_id": buy_order_id,
        "sell_order_id": sell_order_id,
    }

    _append_record(record)
    _clear_pending(symbol)


def _append_record(record: dict[str, Any]) -> None:
    """Append one JSON line, atomically, with fsync for durability."""
    os.makedirs(os.path.dirname(LIVE_TRADE_HISTORY_PATH) or ".", exist_ok=True)
    with open(LIVE_TRADE_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _clear_pending(symbol: str) -> None:
    pending = _load_pending()
    pending.pop(symbol, None)
    _save_pending(pending)


def load_live_trades(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Read the LIVE ledger, newest first (MetricsManager's data source)."""
    trades: list[dict[str, Any]] = []
    try:
        with open(LIVE_TRADE_HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        return []
    trades.sort(key=lambda t: t.get("closed_at", ""), reverse=True)
    return trades if limit is None else trades[:limit]
