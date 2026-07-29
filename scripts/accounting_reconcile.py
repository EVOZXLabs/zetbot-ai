"""Startup accounting reconciliation for ZetBot AI.

Detects and repairs inconsistencies between the four accounting files
written by different code paths:

- ``paper_state.json``   — authoritative wallet balance + position objects
- ``paper_balance.json`` — exported metrics snapshot (balance, PnL, return %)
- ``positions.json``     — position records for Telegram/reporting
- ``paper_orders.json``  — full order history (canonical for open detection)

Common drift scenarios:
  1. Monitor closed a position but ``paper_balance.json`` equity is stale
     (set to cash, ignoring remaining open positions' unrealized PnL).
  2. ``initial_balance`` differs between ``paper_state.json`` and
     ``paper_balance.json``.
  3. ``total_return_pct`` was computed from a stale equity value.
  4. ``profit_factor`` is ``Infinity`` / ``NaN`` (no losing trades).
  5. ``paper_orders.json`` has FILLED BUYs without SELLs, but
     ``positions.json`` shows all positions CLOSED (three-writer drift).

This module is called once at startup, BEFORE the paper engine restores
state, so the repair happens before any user can query ``/balance`` or
``/wallet``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from scripts.position_status import is_open

logger = logging.getLogger(__name__)

_STATE_PATH = "data/paper_state.json"
_BALANCE_PATH = "data/paper_balance.json"
_POSITIONS_PATH = "data/positions.json"
_ORDERS_PATH = "data/paper_orders.json"


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def reconcile(
    logger_obj: logging.Logger | None = None,
    account_balance: float = 10_000.0,
) -> dict[str, Any]:
    """Run all reconciliation checks and repairs.

    Returns a dict of findings for testing / logging::

        {
            "initial_balance_mismatch": False,
            "stale_equity": False,
            "profit_factor_fixed": False,
            "repairs_applied": 0,
        }
    """
    log = logger_obj or logger
    findings: dict[str, Any] = {
        "initial_balance_mismatch": False,
        "stale_equity": False,
        "profit_factor_fixed": False,
        "three_writer_drift": False,
        "fresh_account_initialized": False,
        "repairs_applied": 0,
    }

    state = _read_json(_STATE_PATH)
    pb = _read_json(_BALANCE_PATH)
    pos_data = _read_json(_POSITIONS_PATH)

    if not state and not pb:
        # Brand-new account (first run) or right after
        # scripts/reset_paper_state.py — neither accounting file
        # exists yet. Previously this was just skipped, which left
        # /status, /balance and /wallet showing $0.00 and a
        # nonsensical "-100% all-time" (since the initial_balance
        # fallback used for the % calc is 10,000 while cash/equity
        # default to 0 when the file is missing) until the bot's
        # first full pipeline cycle happened to write these files.
        # Fund the account immediately instead, so Telegram always
        # shows accurate numbers from the very first query.
        log.info(
            f"[RECONCILE] No accounting files found — initializing "
            f"fresh account with balance={account_balance:.2f}"
        )
        fresh_state = {"initial_balance": account_balance, "balance": account_balance}
        fresh_pb = {
            "initial_balance": account_balance,
            "final_balance": account_balance,
            "final_equity": account_balance,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "total_return_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "profit_factor": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }
        _write_json(_STATE_PATH, fresh_state)
        _write_json(_BALANCE_PATH, fresh_pb)
        findings["fresh_account_initialized"] = True
        findings["repairs_applied"] += 1
        return findings

    # ------------------------------------------------------------------
    #  1. Detect mismatched initial_balance
    # ------------------------------------------------------------------
    state_initial = state.get("initial_balance", 10_000.0)
    pb_initial = pb.get("initial_balance", 10_000.0)

    if state and pb and state_initial != pb_initial:
        findings["initial_balance_mismatch"] = True
        log.warning(
            f"[RECONCILE] initial_balance mismatch: "
            f"paper_state={state_initial} vs paper_balance={pb_initial} "
            f"— using paper_state value"
        )
        pb["initial_balance"] = state_initial
        findings["repairs_applied"] += 1

    initial = state_initial if state else pb_initial

    # ------------------------------------------------------------------
    #  1b. Detect open positions from paper_orders.json (three-writer fix)
    # ------------------------------------------------------------------
    orders_data = _read_json(_ORDERS_PATH)
    all_orders = orders_data.get("orders", []) if orders_data else []

    # Build set of symbols with FILLED BUY but no matching SELL
    buy_filled: dict[str, dict] = {}   # symbol → latest FILLED BUY order
    sell_filled: set[str] = set()      # symbols that have been sold
    for o in all_orders:
        sym = o.get("symbol", "")
        side = o.get("side", "")
        status = o.get("status", "")
        if side == "BUY" and status == "FILLED":
            buy_filled[sym] = o
        elif side == "SELL" and status in ("FILLED", "CLOSED"):
            sell_filled.add(sym)

    open_by_orders = {
        sym: o for sym, o in buy_filled.items()
        if sym not in sell_filled
    }

    # Cross-validate with positions.json
    pos_list = pos_data.get("positions", []) if pos_data else []
    pos_by_symbol = {p.get("symbol", ""): p for p in pos_list}

    for sym, buy_order in open_by_orders.items():
        pos = pos_by_symbol.get(sym)
        if pos is None or not is_open(pos.get("status")):
            # Orders say OPEN but positions.json says CLOSED/missing
            log.warning(
                f"[RECONCILE] Three-writer drift: {sym} has FILLED BUY "
                f"(order={buy_order.get('id', '?')}) but positions.json "
                f"shows status={pos.get('status') if pos else 'MISSING'}"
            )
            findings["three_writer_drift"] = True
            findings["repairs_applied"] += 1

            # Repair positions.json: re-create the position from order data
            if pos is None:
                # Build a minimal position record from the order
                repaired_pos = {
                    "symbol": sym,
                    "order_id": buy_order.get("id", ""),
                    "quantity": buy_order.get("quantity", 0.0),
                    "remaining_qty": buy_order.get("filled_quantity", 0.0),
                    "entry_price": buy_order.get("fill_price", 0.0),
                    "current_price": buy_order.get("fill_price", 0.0),
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "total_pnl": 0.0,
                    "cost_basis": buy_order.get("total_cost", 0.0),
                    "status": "OPEN",
                    "opened_at": buy_order.get("filled_at", ""),
                    "tp1": 0.0,
                    "tp2": 0.0,
                    "tp3": 0.0,
                    "stop_loss": 0.0,
                    "position_size_usdt": 0.0,
                }
                pos_list.append(repaired_pos)
            else:
                # Repair: restore position data from the order (price,
                # quantity, and status) so the canonical accounting
                # function (which reads positions.json) computes
                # correct equity.
                pos["status"] = "OPEN"
                pos["current_price"] = buy_order.get("fill_price", pos.get("entry_price", 0.0))
                pos["remaining_qty"] = buy_order.get("filled_quantity", pos.get("quantity", 0.0))
                pos["unrealized_pnl"] = 0.0
                pos["realized_pnl"] = 0.0
                pos["total_pnl"] = 0.0

    if open_by_orders:
        log.info(
            f"[RECONCILE] {len(open_by_orders)} open position(s) detected "
            f"from paper_orders.json"
        )

    # Write repaired positions.json if three-writer drift was detected
    if findings.get("three_writer_drift"):
        repaired_positions = {
            "generated": pos_data.get("generated", "") if pos_data else "",
            "total_positions": len(pos_list),
            "active_count": sum(
                1 for p in pos_list if is_open(p.get("status"))
            ),
            "closed_count": sum(
                1 for p in pos_list if not is_open(p.get("status"))
            ),
            "positions": pos_list,
        }
        _write_json(_POSITIONS_PATH, repaired_positions)
        log.info("[RECONCILE] Repaired positions.json for three-writer drift")

    # ------------------------------------------------------------------
    #  2. Rebuild equity via canonical MetricsManager.compute_snapshot()
    # ------------------------------------------------------------------
    if pb:
        from scripts.metrics_manager import MetricsManager

        cash = pb.get("final_balance", 0.0)
        realized = pb.get("realized_pnl", 0.0)
        positions = pos_list  # use the (possibly repaired) list
        open_positions = [p for p in positions if is_open(p.get("status"))]

        snapshot = MetricsManager.compute_snapshot(
            cash=cash,
            realized_pnl=realized,
            initial_balance=initial,
            open_positions=open_positions,
            total_trades=pb.get("total_trades", 0),
            winning_trades=pb.get("winning_trades", 0),
            losing_trades=pb.get("losing_trades", 0),
            win_rate=pb.get("win_rate", 0.0),
            profit_factor=pb.get("profit_factor", 0.0),
            gross_profit=pb.get("gross_profit", 0.0),
            gross_loss=pb.get("gross_loss", 0.0),
        )

        file_equity = pb.get("final_equity", cash)

        # Detect stale equity: file says equity == cash but positions exist
        if open_positions and abs(file_equity - cash) < 0.01:
            findings["stale_equity"] = True
            log.warning(
                f"[RECONCILE] Stale equity detected: file={file_equity} "
                f"but {len(open_positions)} open position(s) with "
                f"unrealized=${snapshot.unrealized_pnl:+.2f} — repairing"
            )

        # Write computed values from canonical snapshot — only count
        # as a repair if a value actually changed.
        snap_fields = {
            "unrealized_pnl": round(snapshot.unrealized_pnl, 2),
            "final_equity": round(snapshot.equity, 2),
            "net_pnl": round(snapshot.net_pnl, 2),
            "total_return_pct": round(snapshot.total_return_pct, 2),
        }
        for key, val in snap_fields.items():
            if pb.get(key) != val:
                pb[key] = val
                findings["repairs_applied"] += 1

        # ------------------------------------------------------------------
        #  3. Fix Infinity / NaN profit_factor
        # ------------------------------------------------------------------
        pf = pb.get("profit_factor", 0.0)
        if pf != pf or pf == float("inf") or pf == float("-inf"):
            findings["profit_factor_fixed"] = True
            log.warning(
                f"[RECONCILE] Invalid profit_factor={pf} — resetting to 0.0"
            )
            pb["profit_factor"] = 0.0
            findings["repairs_applied"] += 1

        # Ensure initial_balance is in paper_balance.json
        if pb.get("initial_balance") != initial:
            pb["initial_balance"] = initial
            findings["repairs_applied"] += 1

        # Write file only if any repair was applied
        if findings["repairs_applied"] > 0:
            _write_json(_BALANCE_PATH, pb)
            log.info(
                f"[RECONCILE] Accounting reconciled: "
                f"balance=${cash:,.2f} equity=${snapshot.equity:,.2f} "
                f"unrealized=${snapshot.unrealized_pnl:+.2f} "
                f"return={snapshot.total_return_pct:+.2f}% "
                f"({len(open_positions)} open, "
                f"{findings['repairs_applied']} repair(s))"
            )
        else:
            log.debug(
                f"[RECONCILE] All clean — "
                f"balance=${cash:,.2f} equity=${snapshot.equity:,.2f} "
                f"({len(open_positions)} open)"
            )

    return findings
