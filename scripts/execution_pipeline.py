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
    """

    def __init__(
        self,
        provider: ExecutionProvider,
        quote_currency: str = "USDT",
    ) -> None:
        self._provider = provider
        self._quote = quote_currency

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def execute_plan(self, plan: dict[str, Any]) -> Optional[OrderResult]:
        """Execute a single trade plan (BUY order).

        This is the shared entry point for both paper and live BUY orders.
        The provider handles the actual submission (simulated or real).
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

            emit_event(PipelineEvent("TP_TRIGGERED", symbol, price=tp_price, qty=sell_qty))

            tp_result = self._sell(symbol, tp_price, sell_qty)
            if tp_result.status != "FILLED":
                if tp_result.error and str(tp_result.error).startswith("NO_BALANCE"):
                    # Exchange already holds ~0 of this asset — the position
                    # is gone (manual sell, or a sibling order got there
                    # first). Retrying the same amount every pipeline cycle
                    # will never succeed, so stop tracking it as open.
                    _log.warning(
                        "TP sell skipped for %s: %s — marking remaining "
                        "quantity as closed instead of retrying forever",
                        symbol, tp_result.error,
                    )
                    remaining = 0
                    result[hit_key] = True
                    break
                _log.warning("TP sell failed for %s: %s", symbol, tp_result.error)
                continue

            emit_event(PipelineEvent("EXIT_SUBMITTED", symbol, side="SELL_TP", result=tp_result.to_dict()))

            cost_part = cost_basis * (sell_qty / qty) if qty > 0 else 0
            pnl = (tp_result.cost or sell_qty * tp_price) - cost_part
            realized_pnl += pnl
            remaining -= sell_qty
            result[hit_key] = True

        # --- Process SL ---
        if sl_hit and remaining > 0:
            emit_event(PipelineEvent("SL_TRIGGERED", symbol, price=current_price, qty=remaining))
            sl_result = self._sell(symbol, current_price, remaining)
            if sl_result.status == "FILLED":
                emit_event(PipelineEvent("EXIT_SUBMITTED", symbol, side="SELL_SL", result=sl_result.to_dict()))
                cost_part = cost_basis * (remaining / qty) if qty > 0 else 0
                close_pnl = (sl_result.cost or remaining * current_price) - cost_part
                realized_pnl += close_pnl
                remaining = 0
                result["status"] = "STOPPED"
            elif sl_result.error and str(sl_result.error).startswith("NO_BALANCE"):
                _log.warning(
                    "SL sell skipped for %s: %s — marking remaining "
                    "quantity as closed instead of retrying forever",
                    symbol, sl_result.error,
                )
                remaining = 0
                result["status"] = "STOPPED"
            else:
                _log.warning("SL sell failed for %s: %s", symbol, sl_result.error)

        # --- Update position state ---
        result["remaining_qty"] = round(remaining, 8)
        result["realized_pnl"] = round(realized_pnl, 2)

        if remaining <= 0:
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

    def _sell(self, symbol: str, price: float, qty: float) -> OrderResult:
        request = OrderRequest(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            amount=qty,
            price=price,
            metadata={"source": "execution_pipeline"},
        )
        return self._provider.execute_sell(request)
