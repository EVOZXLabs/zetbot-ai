"""Unified Execution Pipeline — shared business logic for Paper and Live trading.

All TP/SL logic, position management, and portfolio updates use the SAME
code path regardless of mode. Only the ExecutionProvider differs.

Flow::

    ExecutionPipeline.run(plans, provider)
        ├── execute_buy_orders()    — validate + execute BUY via provider
        ├── reconcile_positions()   — check TP/SL for all open positions
        └── update_portfolio()      — persist state to files
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from scripts.execution_provider import (
    ExecutionProvider,
    OrderRequest,
    OrderResult,
    PipelineEvent,
    emit_event,
    EVENT_LOG_PATH,
)
from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES

_log = logging.getLogger("ZetBot")

TP1_SELL_PCT = 0.30
TP2_SELL_PCT = 0.30
TP3_SELL_PCT = 0.40


class ExecutionPipeline:
    """Unified execution pipeline used by BOTH paper and live modes.

    Args:
        provider: ExecutionProvider (Paper or Live)
        quote_currency: e.g. "USDT"
        notifier: optional Notifier for Telegram TP/SL notifications
    """

    def __init__(
        self,
        provider: ExecutionProvider,
        quote_currency: str = "USDT",
        notifier: Any = None,
    ) -> None:
        self._provider = provider
        self._quote = quote_currency
        self._notifier = notifier

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def execute_plan(self, plan: dict[str, Any]) -> Optional[OrderResult]:
        """Execute a single trade plan (BUY order).

        This is the shared entry point for both paper and live BUY orders.
        The provider handles the actual submission (simulated or real).

        Safety guards applied here (before reaching the provider):

        1. Balance pre-flight — the estimated order cost (qty × price +
           conservative fee buffer) must not exceed the provider's
           available balance.  This is a pipeline-level guard so rejection
           is logged and reported as an OrderResult regardless of which
           provider (paper or live) is in use.

        2. Per-symbol BUY lock — reuses the same per-symbol RLock that
           ``exit_gate`` uses for SELL paths.  A BUY for a symbol whose
           SELL is in progress (or vice-versa) is serialized rather than
           racing; if the symbol already has an active SELL leg the BUY is
           rejected with a clear message.
        """
        symbol = plan.get("symbol", "")
        if not symbol:
            return None

        emit_event(PipelineEvent("SIGNAL_GENERATED", symbol, plan=plan))

        entry_price = plan.get("entry_price", 0.0)
        qty = plan.get("quantity", 0.0)
        if qty <= 0 and entry_price > 0:
            qty = plan.get("position_size_usdt", 0.0) / entry_price

        if qty <= 0 or entry_price <= 0:
            emit_event(PipelineEvent("ORDER_REJECTED", symbol, reason="invalid plan"))
            return None

        # ------------------------------------------------------------------
        #  GUARD 1 — Balance pre-flight check (pipeline level)
        # ------------------------------------------------------------------
        # Estimate total cost including a 0.15 % fee buffer so we never
        # attempt a BUY we already know will fail due to insufficient funds.
        estimated_cost = qty * entry_price * 1.0015
        available = self._provider.get_balance()
        if available is not None and estimated_cost > available:
            reason = (
                f"Insufficient balance: need ~{estimated_cost:.2f} "
                f"{self._quote}, have {available:.2f} {self._quote}"
            )
            _log.warning("BUY rejected (balance preflight) for %s: %s", symbol, reason)
            emit_event(PipelineEvent("ORDER_REJECTED", symbol, reason=reason))
            dummy = OrderRequest(symbol=symbol, side="BUY", amount=qty, price=entry_price)
            return OrderResult.rejected(dummy, reason, executor="pipeline")

        # ------------------------------------------------------------------
        #  GUARD 2 — Per-symbol trading lock
        # ------------------------------------------------------------------
        # The lock ensures a BUY for a symbol cannot run concurrently with a
        # SELL (or another BUY) for the same symbol.  We acquire
        # non-blocking; if the lock is held by an ongoing SELL we reject
        # immediately rather than queuing an order whose preconditions may
        # have changed by the time the lock is released.
        from scripts.exit_gate import lock_for  # noqa: PLC0415
        sym_lock = lock_for(symbol)
        acquired = sym_lock.acquire(blocking=False)
        if not acquired:
            reason = (
                f"Trading locked for {symbol}: another BUY/SELL is in progress. "
                "Order rejected to prevent race condition."
            )
            _log.warning("BUY rejected (symbol lock) for %s", symbol)
            emit_event(PipelineEvent("ORDER_REJECTED", symbol, reason=reason))
            dummy = OrderRequest(symbol=symbol, side="BUY", amount=qty, price=entry_price)
            return OrderResult.rejected(dummy, reason, executor="pipeline")

        try:
            request = OrderRequest(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                amount=qty,
                price=entry_price,
                stop_loss=plan.get("stop_loss"),
                take_profit=plan.get("tp1"),
                metadata={
                    "tp1": plan.get("tp1", 0),
                    "tp2": plan.get("tp2", 0),
                    "tp3": plan.get("tp3", 0),
                    "stop_loss": plan.get("stop_loss", 0),
                    "position_size_usdt": plan.get("position_size_usdt", 0),
                    "signal_time": plan.get("signal_time", ""),
                },
            )

            emit_event(PipelineEvent("ORDER_VALIDATED", symbol, request=request.to_dict()))
            result = self._provider.execute_buy(request)

            if result.status == "FILLED":
                emit_event(PipelineEvent("ORDER_FILLED", symbol, result=result.to_dict()))
            elif result.status == "REJECTED":
                emit_event(PipelineEvent("ORDER_REJECTED", symbol, reason=result.error))
            else:
                emit_event(PipelineEvent("ORDER_SUBMITTED", symbol, status=result.status))

            return result
        finally:
            sym_lock.release()

    def reconcile_position(
        self,
        symbol: str,
        current_price: float,
        position: dict[str, Any],
        plan: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Check TP/SL for one position and execute exits if triggered.

        Shared logic for both paper and live modes.
        Returns updated position dict, or None if position closed.

        The position dict shape mirrors what position_manager.PositionSimulator
        produces, but the exit execution goes through the provider.
        """
        status = position.get("status", "OPEN")
        if status not in OPEN_STATUSES:
            return position

        # Currency guard: never TP/SL-reconcile a position whose symbol
        # quote does not match the account quote currency. A restored
        # legacy symbol like ``BTC/USDT`` on an Indodax/IDR account would
        # otherwise be closed on prices fetched for a different market
        # (or stale last-known prices), booking a bogus PnL in the wrong
        # units. Log once and leave the position untouched — the operator
        # can close it manually or correct the record.
        if self._quote:
            _sym_quote = symbol.split("/")[1].upper() if "/" in symbol else ""
            if _sym_quote and _sym_quote != self._quote.upper():
                _log.warning(
                    "Skipping TP/SL for %s: symbol quote %s != account "
                    "quote %s (mismatched/legacy position)",
                    symbol, _sym_quote, self._quote.upper(),
                )
                return position

        entry = position.get("entry_price", 0)
        qty = position.get("quantity", 0)
        remaining = position.get("remaining_qty", qty)
        cost_basis = position.get("cost_basis", entry * qty)
        stop_loss = position.get("stop_loss", 0)
        tp1 = position.get("tp1", 0)
        tp2 = position.get("tp2", 0)
        tp3 = position.get("tp3", 0)

        if current_price <= 0:
            return position

        # --- Determine TP levels hit ---
        tp1_hit = bool(position.get("tp1_hit", False)) or (tp1 > 0 and current_price >= tp1)
        tp2_hit = bool(position.get("tp2_hit", False)) or (tp2 > 0 and current_price >= tp2)
        tp3_hit = bool(position.get("tp3_hit", False)) or (tp3 > 0 and current_price >= tp3)

        # --- Determine SL hit ---
        sl_hit = stop_loss > 0 and current_price <= stop_loss

        # --- Build result ---
        result = dict(position)
        realized_pnl = float(position.get("realized_pnl", 0))

        # --- Process TP hits sequentially ---
        tp_config = [
            (tp1_hit, not position.get("tp1_hit", False), tp1, 0.30, "tp1_hit"),
            (tp2_hit, not position.get("tp2_hit", False), tp2, 0.30, "tp2_hit"),
            (tp3_hit, not position.get("tp3_hit", False), tp3, 0.40, "tp3_hit"),
        ]

        for is_hit, is_new, tp_price, fraction, hit_key in tp_config:
            if not is_hit or not is_new:
                continue
            if tp_price <= 0:
                continue

            sell_qty = qty * fraction
            sell_qty = min(sell_qty, remaining)
            if sell_qty <= 0:
                continue

            # A concurrent reconciler (paper monitor vs pipeline) may have
            # already sold this level and persisted the hit flag while we
            # were fetching prices. Re-read the authoritative state so two
            # in-process threads can never both sell the same quantity.
            if self._level_already_executed(symbol, hit_key, remaining):
                continue

            emit_event(PipelineEvent("TP_TRIGGERED", symbol, price=tp_price, qty=sell_qty))

            # Write-ahead: persist the anticipated post-exit state BEFORE
            # submitting the sell. If the process dies right after the
            # order fills, positions.json already shows the level as hit,
            # so a restart can never re-sell the same quantity (the crash
            # window that double-sold CELR/IDR). Rolled back if the sell
            # itself fails.
            pending = dict(result)
            pending[hit_key] = True
            pending["remaining_qty"] = round(remaining - sell_qty, 8)
            self._write_ahead(symbol, pending)

            tp_result = self._sell(symbol, tp_price, sell_qty, exit_level=hit_key)
            if tp_result.status != "FILLED":
                _log.warning("TP sell failed for %s: %s — rolling back exit state", symbol, tp_result.error)
                self._write_ahead(symbol, dict(result))
                continue

            emit_event(PipelineEvent("EXIT_SUBMITTED", symbol, side="SELL_TP", result=tp_result.to_dict()))

            cost_part = cost_basis * (sell_qty / qty) if qty > 0 else 0
            pnl = (tp_result.cost or sell_qty * tp_price) - cost_part
            realized_pnl += pnl
            remaining -= sell_qty
            result[hit_key] = True

            if self._notifier is not None:
                try:
                    from datetime import datetime, timezone, timedelta
                    entry_time = position.get("entry_time") or position.get("opened_at", "")
                    holding = timedelta()
                    if entry_time:
                        try:
                            dt = datetime.fromisoformat(entry_time.split("+")[0].split("Z")[0])
                            holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
                            if holding.total_seconds() < 0:
                                holding = timedelta()
                        except (ValueError, TypeError):
                            pass
                    level_label = hit_key.upper().replace("_HIT", "") if hit_key else ""
                    self._notifier.notify_take_profit(
                        symbol=symbol,
                        entry_price=entry,
                        exit_price=tp_price,
                        profit=pnl,
                        holding_time=holding,
                        level=level_label,
                    )
                except Exception:
                    pass

        # --- Process SL ---
        if sl_hit and remaining > 0:
            # Same guard as the TP levels: skip if a concurrent reconciler
            # already drained the position.
            if not self._position_drained(symbol):
                emit_event(PipelineEvent("SL_TRIGGERED", symbol, price=current_price, qty=remaining))

                # Write-ahead the full close before selling (see above).
                pending = dict(result)
                pending["status"] = "STOPPED"
                pending["remaining_qty"] = 0.0
                self._write_ahead(symbol, pending)

                sl_result = self._sell(symbol, current_price, remaining, exit_level="sl")
                if sl_result.status == "FILLED":
                    emit_event(PipelineEvent("EXIT_SUBMITTED", symbol, side="SELL_SL", result=sl_result.to_dict()))
                    cost_part = cost_basis * (remaining / qty) if qty > 0 else 0
                    close_pnl = (sl_result.cost or remaining * current_price) - cost_part
                    realized_pnl += close_pnl
                    remaining = 0
                    result["status"] = "STOPPED"

                    if self._notifier is not None:
                        try:
                            from datetime import datetime, timezone, timedelta
                            entry_time = position.get("entry_time") or position.get("opened_at", "")
                            holding = timedelta()
                            if entry_time:
                                try:
                                    dt = datetime.fromisoformat(entry_time.split("+")[0].split("Z")[0])
                                    holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
                                    if holding.total_seconds() < 0:
                                        holding = timedelta()
                                except (ValueError, TypeError):
                                    pass
                            self._notifier.notify_stop_loss(
                                symbol=symbol,
                                entry_price=entry,
                                exit_price=current_price,
                                loss=close_pnl,
                                holding_time=holding,
                            )
                        except Exception:
                            pass
                else:
                    _log.warning("SL sell failed for %s: %s — rolling back exit state", symbol, sl_result.error)
                    self._write_ahead(symbol, dict(result))

        # --- Update position state ---
        result["remaining_qty"] = round(remaining, 8)
        result["realized_pnl"] = round(realized_pnl, 2)

        if remaining <= 0:
            # STOPPED (stop-loss) must survive here — the generic CLOSED
            # label is only for take-profit drains, otherwise every
            # stop-loss exit gets misreported as "Take Profit".
            if result.get("status") not in CLOSED_STATUSES:
                result["status"] = "CLOSED"
            result["remaining_qty"] = 0.0
            result["unrealized_pnl"] = 0.0
            result["total_pnl"] = round(realized_pnl, 2)
            emit_event(PipelineEvent("POSITION_CLOSED", symbol,
                       total_pnl=result["total_pnl"], exit_reason=result.get("status", "CLOSED")))
        else:
            cost_remaining = cost_basis * (remaining / qty) if qty > 0 else 0
            result["unrealized_pnl"] = round(current_price * remaining - cost_remaining, 2)
            result["total_pnl"] = round(realized_pnl + result["unrealized_pnl"], 2)
            result["current_price"] = current_price

        return result

    def close_position(
        self,
        symbol: str,
        price: float,
        qty: float,
        reason: str = "manual",
    ) -> Optional[OrderResult]:
        """Close a position entirely (market sell)."""
        emit_event(PipelineEvent("EXIT_SUBMITTED", symbol, reason=reason, qty=qty))
        result = self._sell(symbol, price, qty)
        if result.status == "FILLED":
            emit_event(PipelineEvent("POSITION_CLOSED", symbol, reason=reason, result=result.to_dict()))
        return result

    def get_provider(self) -> ExecutionProvider:
        return self._provider

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _write_ahead(self, symbol: str, state: dict[str, Any]) -> None:
        """Persist ``state`` for ``symbol`` into positions.json (fail-soft).

        Write-ahead: called BEFORE a market sell so that a crash between
        the order fill and the caller's own persist step can never make a
        restart re-sell the same quantity. Never raises — a failed state
        write must not break trading.
        """
        try:
            from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
            merge_positions([state])
        except Exception:
            _log.debug("Write-ahead persist failed for %s", symbol)

    def _authoritative(self, symbol: str) -> Optional[dict[str, Any]]:
        """Re-read the position record from positions.json (or None)."""
        try:
            from scripts.exit_gate import load_position  # noqa: PLC0415
            return load_position(symbol)
        except Exception:
            return None

    def _level_already_executed(self, symbol: str, hit_key: str, remaining: float) -> bool:
        """True when a concurrent reconciler already sold this TP level."""
        fresh = self._authoritative(symbol)
        if fresh is None:
            return False
        if fresh.get(hit_key, False):
            return True
        fresh_remaining = float(
            fresh.get("remaining_qty", fresh.get("quantity", 0)) or 0
        )
        return fresh_remaining <= 0 or fresh_remaining < remaining

    def _position_drained(self, symbol: str) -> bool:
        """True when a concurrent reconciler already closed the position."""
        fresh = self._authoritative(symbol)
        if fresh is None:
            return False
        if fresh.get("status") not in OPEN_STATUSES:
            return True
        fresh_remaining = float(
            fresh.get("remaining_qty", fresh.get("quantity", 0)) or 0
        )
        return fresh_remaining <= 0

    def _sell(
        self,
        symbol: str,
        price: float,
        qty: float,
        exit_level: str = "",
    ) -> OrderResult:
        request = OrderRequest(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            amount=qty,
            price=price,
            metadata={
                "source": "execution_pipeline",
                "exit_level": exit_level,
            },
        )
        return self._provider.execute_sell(request)
