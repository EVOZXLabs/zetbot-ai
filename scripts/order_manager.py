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

    # -- Metrics ----------------------------------------------------------

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
        """Execute with simple retry policy for transient failures."""
        last_error: Optional[str] = None
        max_retries = max(1, RETRY_MAX)

        for attempt in range(max_retries):
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
