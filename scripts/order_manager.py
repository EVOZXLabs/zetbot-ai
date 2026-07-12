"""OrderManager — the ONLY component allowed to submit orders.

Every order MUST pass through RiskManager before execution.
ExecutionEngine selects the correct executor based on trading mode.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from scripts.execution_engine import (
    AUDIT_PATH,
    IExecutionEngine,
    OrderRequest,
    OrderResult,
    AuditEntry,
    ExecutionEngine,
    ExecutionMetrics,
    _generate_id,
    _now,
    append_audit,
)


class OrderVerificationError(Exception):
    """Raised when a retry cannot verify whether the previous LIVE order
    attempt actually reached the exchange. Retries must stop here rather
    than guess — resubmitting blindly risks a duplicate real-money fill.
    """


RETRY_MAX = 3
RETRY_DELAY_SEC = 1.0


class OrderManager:
    """Central order manager — ONLY component that may submit orders.

    Routes through:
        1. Risk validation
        2. ExecutionEngine (which selects Paper/Simulation/Live executor)
        3. Retry policy on transient failures
        4. Audit trail logging
        5. Execution metrics recording

    Backward-compatible: ``execute(trade_plan_dict)`` works as before,
    returning a dict.  New code can pass ``OrderRequest`` directly and
    receive an ``OrderResult``.
    """

    def __init__(
        self,
        config: Any,
        exchange: Any,
        wallet: Any,
        risk: Any,
        mode: str = "PAPER",
    ) -> None:
        self._config = config
        self._exchange = exchange
        self._wallet = wallet
        self._risk = risk
        self._engine = ExecutionEngine(exchange, config, wallet, mode)
        self._metrics = ExecutionMetrics()

    # -- Public API (IOrderManager) --------------------------------------

    def execute(self, trade_plan: Any, **kwargs: Any) -> Any:
        """Execute an order.

        Accepts both ``OrderRequest`` and ``dict`` (backward compat).
        When given a ``dict``, converts to ``OrderRequest`` internally.

        Returns ``OrderResult`` (or ``dict`` when given a ``dict`` for
        backward compatibility).
        """
        was_dict = isinstance(trade_plan, dict)

        if isinstance(trade_plan, OrderRequest):
            request = trade_plan
        else:
            request = self._plan_to_request(trade_plan) if isinstance(trade_plan, dict) else trade_plan

        # ── 1. Risk validation ──────────────────────────────────────────
        risk_result = self._validate_risk(request)
        if risk_result is not None:
            self._metrics.record(risk_result)
            append_audit(self._result_to_audit(risk_result))
            return risk_result.to_dict() if was_dict else risk_result

        # ── 2. Execute with retry ───────────────────────────────────────
        result = self._execute_with_retry(request)

        # ── 3. Record & audit ──────────────────────────────────────────
        self._metrics.record(result)
        append_audit(self._result_to_audit(result))

        return result.to_dict() if was_dict else result

    def get_orders(self) -> list[dict[str, Any]]:
        """Return all trade plans from the audit trail / stored plans."""
        path = "data/trade_plan.json"
        try:
            with open(path) as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("plans", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    # -- Mode management --------------------------------------------------

    @property
    def mode(self) -> str:
        return self._engine.mode

    def set_mode(self, mode: str) -> None:
        self._engine.set_mode(mode)

    def enable_live(self) -> None:
        self._engine.enable_live()

    def disable_live(self) -> None:
        self._engine.disable_live()

    def is_live_enabled(self) -> bool:
        return self._engine.is_live_enabled()

    def validate_live_ready(self) -> Optional[str]:
        return self._engine.validate_live_ready()

    # -- State sync --------------------------------------------------------

    def sync_paper_state(self, result: Any) -> None:
        """Sync an OrderResult to the canonical paper state files.

        Writes to ``paper_orders.json``, ``paper_balance.json``, and
        ``positions.json`` so that Telegram commands see the trade
        immediately.  Safe to call multiple times (the next pipeline
        run will re-derive authoritative state from ``paper_state.json``).
        """
        if self._engine.mode != "PAPER":
            return

        was_dict = isinstance(result, dict)
        if was_dict:
            from scripts.execution_engine import OrderResult  # noqa: PLC0415
            # best-effort dict -> OrderResult conversion
            sym = result.get("symbol", "?")
            side = result.get("side", "?")
            amt = result.get("filled_amount", result.get("amount", 0.0))
            price = result.get("filled_price", result.get("price", 0.0))
            cost = result.get("cost", amt * price)
            status = result.get("status", "UNKNOWN")
            pnl = result.get("net_pnl", 0.0)
        else:
            sym = result.symbol
            side = result.side
            amt = result.filled_amount or result.amount
            price = result.filled_price or result.price
            cost = result.cost or amt * price
            status = result.status
            pnl = getattr(result, 'net_pnl', 0.0)

        if status not in ("FILLED", "EXECUTED"):
            return

        _sync_paper_files(sym, side.upper(), amt, price, cost, pnl)

    @property
    def metrics(self) -> ExecutionMetrics:
        return self._metrics

    def get_metrics_summary(self) -> dict[str, Any]:
        return self._metrics.summary()

    # -- Audit ------------------------------------------------------------

    def get_audit_trail(self, limit: int = 100) -> list[AuditEntry]:
        from scripts.execution_engine import read_audit  # noqa: PLC0415
        return read_audit(limit)

    # -- Internal ---------------------------------------------------------

    def _plan_to_request(self, plan: dict[str, Any]) -> OrderRequest:
        """Convert a trade plan dict (from risk manager) to OrderRequest."""
        return OrderRequest(
            trace_id=str(uuid.uuid4()),
            symbol=plan.get("symbol", ""),
            side="BUY",
            type="MARKET",
            amount=plan.get("quantity", plan.get("position_size_usdt", 0.0))
                   / max(plan.get("entry_price", 1.0), 0.0001),
            price=plan.get("entry_price"),
            stop_loss=plan.get("stop_loss"),
            take_profit=plan.get("tp1"),
            metadata={
                k: plan[k] for k in plan
                if k not in ("symbol", "entry_price", "quantity",
                             "position_size_usdt", "stop_loss", "tp1")
            },
        )

    def _validate_risk(self, request: OrderRequest) -> Optional[OrderResult]:
        """Check risk approval. Returns an OrderResult if rejected."""
        # Allow admins to bypass (for manual overrides)
        if request.metadata.get("bypass_risk"):
            return None

        try:
            decisions = self._risk.get_approved()
            if not decisions:
                return OrderResult.rejected(
                    request, "No approved decisions available.",
                )
            # Check that this specific symbol was approved
            matching = [d for d in decisions if d.get("symbol") == request.symbol]
            if not matching:
                return OrderResult.rejected(
                    request,
                    f"Symbol {request.symbol} not in approved decisions.",
                )
        except Exception as exc:
            return OrderResult.rejected(
                request, f"Risk validation error: {exc}",
            )
        return None

    def _execute_with_retry(self, request: OrderRequest) -> OrderResult:
        """Execute with a retry policy for transient failures.

        For LIVE orders, every retry FIRST checks the exchange for an
        order already tagged with this request's ``client_order_id``.
        A network timeout doesn't mean the order failed — it may have
        filled on the exchange while the response was lost. Resubmitting
        blindly in that case would buy/sell twice. If we can't verify
        either way, we stop and surface the ambiguity instead of guessing.
        """
        last_error: Optional[str] = None
        max_retries = max(1, RETRY_MAX)
        is_live = self._engine.mode == "LIVE"

        for attempt in range(max_retries):
            if attempt > 0 and is_live:
                try:
                    existing = self._find_existing_live_order(request)
                except OrderVerificationError as exc:
                    return OrderResult(
                        order_id=_generate_id("unverified_"),
                        trace_id=request.trace_id,
                        execution_id=_generate_id("exe_"),
                        status="FAILED",
                        symbol=request.symbol,
                        side=request.side,
                        type=request.type,
                        amount=request.amount,
                        error=(
                            f"Retry aborted — could not confirm whether the "
                            f"previous attempt reached the exchange ({exc}). "
                            f"Check manually before resubmitting "
                            f"(client_order_id={request.client_order_id})."
                        ),
                        retries=attempt,
                        executor="order_manager",
                        exchange=self._exchange.name,
                        mode=self._engine.mode,
                        timestamp=_now(),
                    )
                if existing is not None:
                    return self._result_from_existing_order(
                        request, existing, attempt,
                    )

            result = self._engine.execute(request)

            if result.status not in ("FAILED",):
                return result

            last_error = result.error
            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY_SEC * (attempt + 1))

        # All retries exhausted
        return OrderResult(
            order_id=_generate_id("fail_"),
            trace_id=request.trace_id,
            execution_id=_generate_id("exe_"),
            status="FAILED",
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            amount=request.amount,
            error=f"All {max_retries} retries failed. Last: {last_error}",
            latency_ms=0.0,
            retries=max_retries,
            executor="order_manager",
            exchange=self._exchange.name,
            mode=self._engine.mode,
            timestamp=_now(),
        )

    def _find_existing_live_order(
        self, request: OrderRequest,
    ) -> Optional[dict[str, Any]]:
        """Look up the exchange for an order already tagged with this
        request's ``client_order_id``.

        Returns the matching ccxt order dict if found, ``None`` if the
        lookup succeeded and genuinely found nothing. Raises
        ``OrderVerificationError`` if the lookup itself could not be
        completed reliably (in which case the caller must NOT retry).
        """
        try:
            provider = self._exchange.get_provider()
            ex = provider._get_exchange()
        except Exception as exc:
            raise OrderVerificationError(
                f"cannot reach exchange to verify: {exc}",
            ) from exc

        client_id = request.client_order_id
        symbol = request.symbol
        candidates: list[dict[str, Any]] = []

        try:
            if provider.has("fetchOpenOrders"):
                candidates.extend(ex.fetch_open_orders(symbol) or [])
            if provider.has("fetchClosedOrders"):
                candidates.extend(ex.fetch_closed_orders(symbol, limit=20) or [])
            elif provider.has("fetchOrders"):
                candidates.extend(ex.fetch_orders(symbol, limit=20) or [])
        except Exception as exc:
            raise OrderVerificationError(
                f"order lookup failed: {exc}",
            ) from exc

        if not provider.has("fetchOpenOrders") and not (
            provider.has("fetchClosedOrders") or provider.has("fetchOrders")
        ):
            # No way to look anything up at all — never safe to guess.
            raise OrderVerificationError(
                f"{provider.name} supports no order-lookup method",
            )

        for order in candidates:
            tag = order.get("clientOrderId") or order.get("client_order_id")
            if tag and str(tag) == client_id:
                return order
        return None

    def _result_from_existing_order(
        self, request: OrderRequest, order: dict[str, Any], retries: int,
    ) -> OrderResult:
        """Convert a previously-found exchange order into an OrderResult
        instead of resubmitting it."""
        status_map = {
            "closed": "FILLED",
            "open": "EXECUTED",
            "canceled": "CANCELLED",
            "cancelled": "CANCELLED",
            "expired": "CANCELLED",
            "rejected": "REJECTED",
        }
        status = status_map.get(str(order.get("status", "")).lower(), "EXECUTED")
        filled = float(order.get("filled", 0) or 0)
        price = float(order.get("average") or order.get("price") or 0)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost", 0) or 0) if isinstance(fee_info, dict) else 0.0
        cost = float(order.get("cost", filled * price) or 0)

        return OrderResult(
            order_id=str(order.get("id", "")),
            trace_id=request.trace_id,
            execution_id=_generate_id("exe_"),
            status=status,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            amount=request.amount,
            filled_amount=filled,
            filled_price=price,
            fee=fee,
            cost=cost,
            error=None,
            retries=retries,
            executor="order_manager_verified",
            exchange=self._exchange.name,
            mode="LIVE",
            timestamp=_now(),
        )

    @staticmethod
    def _result_to_audit(result: OrderResult) -> AuditEntry:
        return AuditEntry(
            trace_id=result.trace_id,
            execution_id=result.execution_id,
            order_id=result.order_id,
            symbol=result.symbol,
            side=result.side,
            amount=result.amount,
            status=result.status,
            executor=result.executor,
            mode=result.mode,
            exchange=result.exchange,
            latency_ms=result.latency_ms,
            retries=result.retries,
            error=result.error,
            timestamp=result.timestamp,
        )


# ---------------------------------------------------------------------------
#  State sync helpers — write OrderResult to canonical paper JSON files
# ---------------------------------------------------------------------------


def _sync_paper_files(
    symbol: str, side: str, amount: float, price: float, cost: float, pnl: float,
) -> None:
    """Write a manual trade to ``paper_orders.json`` and ``paper_balance.json``."""
    from datetime import datetime, timezone  # noqa: PLC0415
    now_ts = datetime.now(timezone.utc).isoformat()
    data_dir = "data"

    # --- Append to paper_orders.json ---
    orders_path = f"{data_dir}/paper_orders.json"
    try:
        with open(orders_path) as f:
            orders_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        orders_data = {"orders": []}
    orders_data.setdefault("orders", []).append({
        "id": f"manual-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": amount,
        "filled_quantity": amount,
        "price": price,
        "fill_price": price,
        "cost": cost,
        "net_pnl": round(pnl, 2),
        "status": "FILLED",
        "created_at": now_ts,
        "filled_at": now_ts,
    })
    try:
        with open(orders_path, "w") as f:
            json.dump(orders_data, f, indent=2)
    except OSError:
        pass

    # --- Update paper_balance.json ---
    bal_path = f"{data_dir}/paper_balance.json"
    try:
        with open(bal_path) as f:
            pb = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pb = {
            "final_balance": 10000.0,
            "final_equity": 10000.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        }

    if side == "BUY":
        pb["final_balance"] = round(pb.get("final_balance", 10000.0) - cost, 2)
        pb["final_equity"] = pb["final_balance"]
    elif side == "SELL":
        proceeds = amount * price
        pb["final_balance"] = round(pb.get("final_balance", 10000.0) + proceeds, 2)
        pb["final_equity"] = pb["final_balance"]
        pb["total_trades"] = pb.get("total_trades", 0) + 1
        if pnl > 0:
            pb["winning_trades"] = pb.get("winning_trades", 0) + 1
        else:
            pb["losing_trades"] = pb.get("losing_trades", 0) + 1
        total = pb.get("total_trades", 0)
        pb["win_rate"] = round(pb.get("winning_trades", 0) / total * 100, 2) if total else 0.0
        pb["realized_pnl"] = round(pb.get("realized_pnl", 0.0) + pnl, 2)
        pb["net_pnl"] = round(pb.get("net_pnl", 0.0) + pnl, 2)

    try:
        with open(bal_path, "w") as f:
            json.dump(pb, f, indent=2)
    except OSError:
        pass
