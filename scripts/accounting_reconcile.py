"""Startup accounting reconciliation for ZetBot AI.

Detects and repairs inconsistencies between the four accounting files
written by different code paths:

- ``paper_balance.json`` — single source of truth for ACCOUNTING values
  (initial_balance, cash, equity, PnL, return % — see AGENTS.md)
- ``paper_state.json``   — wallet ledger; synced TO paper_balance.json on
  startup (never the other way round) so the two can never disagree
- ``positions.json``     — position records for Telegram/reporting
- ``paper_orders.json``  — full order history (canonical for open detection)

Common drift scenarios:
  1. Monitor closed a position but ``paper_balance.json`` equity is stale
     (set to cash, ignoring remaining open positions' unrealized PnL).
  2. ``initial_balance`` differs between ``paper_state.json`` and
     ``paper_balance.json`` — ``paper_balance.json`` wins and
     ``paper_state.json`` is synced to it (legacy USDT-era state with
     ``initial_balance=10000`` must never clobber a correctly-funded IDR
     account whose balance file says 300000).
  3. ``total_return_pct`` was computed from a stale equity value.
  4. ``profit_factor`` is ``Infinity`` / ``NaN`` (no losing trades).
  5. ``paper_orders.json`` has FILLED BUYs without SELLs, but
     ``positions.json`` shows all positions CLOSED (three-writer drift).
  6. Legacy positions / trades quoted in a currency that differs from the
     account's (e.g. ``BTC/USDT`` on an IDR account) pollute the equity /
     trade history — they are dropped with a ``[LEGACY-DATA]`` warning so
     they never corrupt the accounting.

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
    from scripts.paper_state_lock import atomic_write_json as _awj
    _awj(path, data, indent=2, default=str)


def _account_quote() -> str:
    return os.getenv("QUOTE_CURRENCY", "USDT").upper()


def is_legacy(symbol: str, account_quote: str | None = None) -> bool:
    """True when *symbol* is a legacy artifact on this account.

    A symbol with an explicit quote that differs from the account quote
    (e.g. ``BTC/USDT`` on an IDR account) is a leftover from a previous
    exchange/currency and must never pollute the equity, position set or
    trade history.  Symbols without a quote (e.g. ``BTCUSDT``) are
    ambiguous legacy-normalized names and are always kept.
    """
    if not symbol or "/" not in symbol:
        return False
    quote = account_quote or _account_quote()
    return symbol.split("/", 1)[1].upper() != quote


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
        "trade_history_rebuilt": 0,
        "legacy_trades_dropped": 0,
        "legacy_positions_dropped": 0,
        "repairs_applied": 0,
    }

    account_quote = _account_quote()

    state = _read_json(_STATE_PATH)
    pb = _read_json(_BALANCE_PATH)
    pos_data = _read_json(_POSITIONS_PATH)
    data_dir = os.path.dirname(_STATE_PATH) or "."

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
    #  1. initial_balance — paper_balance.json is the single source of
    #     truth (AGENTS.md). Sync paper_state.json to it, never the
    #     other way round: a legacy USDT-era paper_state.json
    #     (initial_balance=10000) must never clobber a correctly-funded
    #     IDR account whose paper_balance.json says 300000.
    # ------------------------------------------------------------------
    state_initial_raw = state.get("initial_balance") if state else None
    pb_initial_raw = pb.get("initial_balance") if pb else None

    if pb_initial_raw is not None:
        initial = float(pb_initial_raw)
    elif state_initial_raw is not None:
        initial = float(state_initial_raw)
    else:
        initial = float(account_balance)

    state_changed = False
    if state is not None and state.get("initial_balance") != initial:
        if state.get("initial_balance") is not None:
            findings["initial_balance_mismatch"] = True
            log.warning(
                f"[RECONCILE] initial_balance mismatch: "
                f"paper_state={state.get('initial_balance')} vs "
                f"paper_balance={pb_initial_raw} — paper_balance.json "
                f"is authoritative, syncing paper_state.json"
            )
        else:
            log.info(
                f"[RECONCILE] paper_state.json missing initial_balance — "
                f"setting to paper_balance value {initial}"
            )
        state["initial_balance"] = initial
        state_changed = True
        findings["repairs_applied"] += 1

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

    dropped_legacy_syms: set[str] = set()

    for sym, buy_order in open_by_orders.items():
        if is_legacy(sym):
            # A FILLED BUY in a mismatched quote currency is a legacy
            # artifact — never re-create a position for it.
            dropped_legacy_syms.add(sym)
            log.warning(
                f"[LEGACY-DATA] ignoring FILLED BUY for legacy symbol "
                f"{sym} (account quote={account_quote})"
            )
            continue
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

    # Drop legacy-quote positions from positions.json and paper_state.json
    # (the wallet ledger) so a mismatched-currency position from a previous
    # exchange can never inflate equity, be reopened by drift repair, or
    # appear in /positions on this account.
    if pos_list:
        for p in pos_list:
            if is_legacy(p.get("symbol", "")):
                dropped_legacy_syms.add(p.get("symbol", ""))
        pos_list = [p for p in pos_list if not is_legacy(p.get("symbol", ""))]
    if state and isinstance(state.get("positions"), dict) and state["positions"]:
        state_pos = {
            s: v for s, v in state["positions"].items() if not is_legacy(s)
        }
        if len(state_pos) != len(state["positions"]):
            for s in state["positions"]:
                if is_legacy(s):
                    dropped_legacy_syms.add(s)
            state["positions"] = state_pos
            state_changed = True

    if dropped_legacy_syms:
        findings["legacy_positions_dropped"] = len(dropped_legacy_syms)
        findings["repairs_applied"] += 1
        log.warning(
            f"[LEGACY-DATA] dropped {len(dropped_legacy_syms)} legacy "
            f"position(s) quoted in a different currency than the account "
            f"({account_quote}): {', '.join(sorted(dropped_legacy_syms))}"
        )

    # Write repaired positions.json if three-writer drift OR legacy
    # positions were dropped.
    if findings.get("three_writer_drift") or dropped_legacy_syms:
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
        log.info("[RECONCILE] Repaired positions.json")

    # ------------------------------------------------------------------
    #  2. Rebuild CLOSED-TRADES store from the authoritative ledgers and
    #     recompute derived trade stats (realized_pnl, total_trades, …).
    #     Done BEFORE the equity snapshot so the cash balance can be
    #     reconciled against the ledger too.
    # ------------------------------------------------------------------
    # CLOSED TRADES have ONE source of truth: paper_trade_history.csv.
    # Rebuild it from the authoritative ledgers (paper_state.json +
    # paper_orders.json) so it can never drift, then recompute
    # paper_balance.json's derived trade stats from it. This guarantees:
    #     closed_trades == len(trade_history)
    #     win_rate     == trade_history win rate
    #     summary      == trade_history == history
    # Legacy positions whose quote currency differs from the account
    # (e.g. BTC/USDT on an IDR account) are dropped with a
    # [LEGACY-DATA] warning so they never corrupt the accounting.
    try:
        from scripts.paper_state_lock import rebuild_trade_history_csv
        n_trades = rebuild_trade_history_csv(data_dir)
    except OSError:
        n_trades = 0
    findings["trade_history_rebuilt"] = n_trades

    trade_pnls: list[float] = []
    csv_path = os.path.join(data_dir, "paper_trade_history.csv")
    try:
        import csv as _csv  # noqa: PLC0415
        with open(csv_path, newline="") as f:
            for row in _csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                if not sym:
                    continue
                if is_legacy(sym):
                    findings["legacy_trades_dropped"] += 1
                    log.warning(
                        "[LEGACY-DATA] ignored mismatched quote trade: "
                        "%s (account quote=%s)", sym, account_quote,
                    )
                    continue
                try:
                    trade_pnls.append(float(row.get("net_pnl", 0) or 0))
                except (ValueError, TypeError):
                    trade_pnls.append(0.0)
    except (FileNotFoundError, OSError):
        pass

    recon_realized = round(sum(trade_pnls), 2)
    recon_total = len(trade_pnls)
    recon_wins = sum(1 for t in trade_pnls if t >= 0)
    recon_losses = recon_total - recon_wins
    recon_wr = (recon_wins / recon_total * 100.0) if recon_total else 0.0
    recon_gp = sum(t for t in trade_pnls if t > 0)
    recon_gl = abs(sum(t for t in trade_pnls if t <= 0))
    recon_pf = (
        recon_gp / recon_gl if recon_gl > 0
        else (0.0 if recon_gp == 0 else 0.0)
    )

    # ------------------------------------------------------------------
    #  3. Rebuild equity via canonical MetricsManager.compute_snapshot()
    # ------------------------------------------------------------------
    if pb:
        from scripts.metrics_manager import MetricsManager

        open_positions = [p for p in pos_list if is_open(p.get("status"))]

        cash = pb.get("final_balance", 0.0)

        # Reconcile the cash balance against the ledger. In a healthy
        # account  cash == initial_balance + realized_pnl - open_cost
        # (every buy spends the full cost, every sell adds proceeds,
        # realized tracks proceeds - cost_of_sold_qty). Only enforced when
        # the trade ledger has at least one closed trade; a zero-trade
        # account may legitimately differ (manual funding adjustment,
        # /reset, …) and its cash is trusted.
        open_cost = 0.0
        for p in open_positions:
            qty = p.get("quantity", 0.0) or 0.0
            remaining = p.get("remaining_qty", qty) or 0.0
            cost = p.get("cost_basis", 0.0) or 0.0
            if qty > 0:
                open_cost += cost * (remaining / qty)
            elif remaining > 0:
                open_cost += cost

        target_cash = initial + recon_realized - open_cost
        if recon_total > 0 and abs(cash - target_cash) >= 1.0:
            log.warning(
                f"[RECONCILE] cash drift: final_balance={cash} vs "
                f"initial({initial}) + realized({recon_realized}) - "
                f"open_cost({open_cost}) = {target_cash:,.2f} — repairing"
            )
            cash = round(target_cash, 2)
            pb["final_balance"] = cash
            findings["repairs_applied"] += 1
            if state is not None and state.get("balance") != cash:
                state["balance"] = cash
                state_changed = True

        realized = pb.get("realized_pnl", 0.0)

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
                f"unrealized={snapshot.unrealized_pnl:+.2f} {account_quote} "
                f"— repairing"
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
        #  4. Fix Infinity / NaN profit_factor
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

        # Sync paper_balance.json derived trade stats from the rebuilt
        # trade history (single source of truth for closed trades).
        for key, val in {
            "realized_pnl": recon_realized,
            "total_trades": recon_total,
            "winning_trades": recon_wins,
            "losing_trades": recon_losses,
            "win_rate": round(recon_wr, 2),
            "profit_factor": round(recon_pf, 2),
            "gross_profit": round(recon_gp, 2),
            "gross_loss": round(recon_gl, 2),
        }.items():
            if pb.get(key) != val:
                pb[key] = val
                findings["repairs_applied"] += 1

        # Write paper_balance.json if any repair was applied.
        if findings["repairs_applied"] > 0:
            _write_json(_BALANCE_PATH, pb)
            log.info(
                f"[RECONCILE] Accounting reconciled: "
                f"balance={cash:,.2f} {account_quote} "
                f"equity={snapshot.equity:,.2f} {account_quote} "
                f"unrealized={snapshot.unrealized_pnl:+.2f} {account_quote} "
                f"return={snapshot.total_return_pct:+.2f}% "
                f"({len(open_positions)} open, "
                f"{findings['repairs_applied']} repair(s), "
                f"{findings['legacy_trades_dropped']} legacy trade(s) "
                f"and {findings['legacy_positions_dropped']} legacy "
                f"position(s) dropped)"
            )

    # Write paper_state.json if initial_balance / balance / legacy
    # positions were repaired there (the engine restores from this file).
    if state_changed:
        _write_json(_STATE_PATH, state)
        log.info("[RECONCILE] Synced paper_state.json to paper_balance.json")

    return findings
