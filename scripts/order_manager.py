"""OrderManager — the ONLY component allowed to submit orders.

Every order MUST pass through RiskManager before execution.
ExecutionEngine selects the correct executor based on trading mode.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

_log = logging.getLogger("ZetBot")

from scripts.execution_engine import (
    AUDIT_PATH,
    IExecutionEngine,
    OrderRequest,
    OrderResult,
    AuditEntry,
    ExecutionEngine,
    ExecutionMetrics,
    _generate_id,
    _map_live_order_status,
    _now,
    append_audit,
)
from scripts.paper_state_lock import paper_state_writes


class OrderVerificationError(Exception):
    """Raised when a retry cannot verify whether the previous LIVE order
    attempt actually reached the exchange. Retries must stop here rather
    than guess — resubmitting blindly risks a duplicate real-money fill.
    """


class LiveArmError(Exception):
    """Raised by ``arm_live()`` when a fresh readiness check fails right
    before flipping the switch — never enable_live() first and check
    after."""


RETRY_MAX = 3
RETRY_DELAY_SEC = 1.0
LIVE_ARMED_PATH = "data/live_armed.json"
PENDING_LIVE_ORDERS_PATH = "data/pending_live_orders.json"
RECONCILE_MAX_WAIT_SEC = 20.0
RECONCILE_POLL_INTERVAL_SEC = 2.0
RECONCILE_TERMINAL_STATUSES = ("FILLED", "CANCELLED", "REJECTED")


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
        safeguard: Any = None,
    ) -> None:
        self._config = config
        self._exchange = exchange
        self._wallet = wallet
        self._risk = risk
        self._safeguard = safeguard
        self._engine = ExecutionEngine(exchange, config, wallet, mode)
        self._metrics = ExecutionMetrics()

    # -- Public API (IOrderManager) --------------------------------------

    def execute(self, trade_plan: Any, **kwargs: Any) -> Any:
        """Execute an order.

        Accepts both ``OrderRequest`` and ``dict`` (backward compat).
        When given a ``dict``, converts to ``OrderRequest`` internally.

        LIVE SELL orders are serialized on the same per-symbol lock used
        by every other exit path (position monitor, pipeline
        reconciliation, protection scheduler) so a manual /sell can never
        race a concurrent TP/SL market sell for the same symbol (BUG-2).

        Returns ``OrderResult`` (or ``dict`` when given a ``dict`` for
        backward compatibility).
        """
        was_dict = isinstance(trade_plan, dict)

        if isinstance(trade_plan, OrderRequest):
            request = trade_plan
        else:
            request = self._plan_to_request(trade_plan) if isinstance(trade_plan, dict) else trade_plan

        if self._engine.mode == "LIVE" and (request.side or "").upper() == "SELL":
            from scripts.exit_gate import exit_guard  # noqa: PLC0415
            with exit_guard(request.symbol):
                return self._execute(request, was_dict)
        return self._execute(request, was_dict)

    def _execute(self, request: OrderRequest, was_dict: bool) -> Any:
        """Execute an already-built order request (holds the per-symbol
        exit lock when it is a LIVE sell — see ``execute``)."""
        # ── 0. SafeGuard check (paused/limits/cooldown) ────────────────
        if self._safeguard is not None and request.side == "BUY":
            ok, reason = self._safeguard.can_open_new_position()
            if not ok:
                return OrderResult.rejected(
                    request, reason, "order_manager",
                )

        # ── 1. Risk validation ──────────────────────────────────────────
        risk_result = self._validate_risk(request)
        if risk_result is not None:
            self._metrics.record(risk_result)
            append_audit(self._result_to_audit(risk_result))
            return risk_result.to_dict() if was_dict else risk_result

        # ── 1b. Cancel resting protection BEFORE a LIVE market sell ────
        # A stale protection leg (full-quantity limit/stop order) could
        # double-fill together with this market sell at the same level.
        # Runs inside the per-symbol exit lock held by ``execute``, i.e.
        # before any concurrent TP/SL exit for the same symbol.
        if self._engine.mode == "LIVE" and (request.side or "").upper() == "SELL":
            try:
                from scripts.protection_manager import ProtectionManager  # noqa: PLC0415
                ProtectionManager(self._exchange, self._config).cancel_protection(
                    request.symbol, reason="manual_sell_pre_execution",
                )
            except Exception:
                pass  # best-effort — _handle_live_protection retries after fill

        # ── 2. Execute with retry ───────────────────────────────────────
        result = self._execute_with_retry(request)

        # ── 2b. Reconciliation (LIVE only) ──────────────────────────────
        # A LIVE create_order() response isn't guaranteed final — poll
        # the exchange until we know the REAL outcome, instead of letting
        # callers (buy/sell commands, pipeline) treat a PENDING/PARTIAL
        # snapshot as settled or as failed.
        if self._engine.mode == "LIVE":
            result = self._reconcile_live_order(result)

        # ── 2c. Position sync + protection (LIVE only) ──────────────────
        if self._engine.mode == "LIVE" and result.status in (
            "FILLED", "PARTIALLY_FILLED",
        ):
            self.sync_live_position(result)
            self._handle_live_protection(request, result)

        # ── 3. Record & audit ──────────────────────────────────────────
        self._metrics.record(result)
        append_audit(self._result_to_audit(result))

        return result.to_dict() if was_dict else result

    def _reconcile_live_order(self, result: OrderResult) -> OrderResult:
        """Poll the exchange until a LIVE order reaches a terminal state
        (FILLED / CANCELLED / REJECTED), or a bounded timeout elapses.

        ``create_order()``'s own response is a snapshot, not a settlement
        guarantee — some exchanges report ``open``/partial fills even for
        market orders. This closes that gap so a PENDING/PARTIALLY_FILLED
        result is never mistaken for "done" (or for "failed").
        """
        if result.status not in ("PENDING", "PARTIALLY_FILLED"):
            return result
        if not result.order_id or not result.symbol:
            return result

        self._persist_pending_order(result)

        provider = self._exchange.get_provider()
        deadline = time.time() + RECONCILE_MAX_WAIT_SEC
        last = result

        while time.time() < deadline:
            time.sleep(RECONCILE_POLL_INTERVAL_SEC)
            try:
                raw = provider.fetch_order(last.order_id, last.symbol)
            except Exception as exc:
                # Couldn't confirm on this attempt — keep the last known
                # state and keep trying until the deadline. Never invent
                # a final status just because one poll failed.
                last = replace(
                    last,
                    error=f"reconciliation check failed (will retry): {exc}",
                )
                continue

            if not raw:
                continue

            status = _map_live_order_status(raw, last.amount)
            filled = float(raw.get("filled", 0) or 0)
            price = float(
                raw.get("average") or raw.get("price") or last.filled_price or 0,
            )
            fee_info = raw.get("fee") or {}
            fee = (
                float(fee_info.get("cost", 0) or 0)
                if isinstance(fee_info, dict) else last.fee
            )
            cost = float(raw.get("cost", filled * price) or 0)

            last = replace(
                last,
                status=status,
                filled_amount=filled,
                filled_price=price,
                fee=fee,
                cost=cost,
                error=None,
            )

            if status in RECONCILE_TERMINAL_STATUSES:
                self._clear_pending_order(last.order_id)
                return last

            self._persist_pending_order(last)

        # Timed out still open/partial. Surface this clearly — the order
        # may well still be live on the exchange; it is NOT safe to treat
        # it as either filled or failed. It stays recorded in
        # data/pending_live_orders.json for manual follow-up.
        return replace(
            last,
            error=(
                (last.error + " " if last.error else "")
                + f"Reconciliation timed out after {RECONCILE_MAX_WAIT_SEC:.0f}s — "
                f"order {last.order_id} may still be open on the exchange; "
                "check manually (see data/pending_live_orders.json)."
            ),
        )

    @staticmethod
    def _persist_pending_order(result: OrderResult) -> None:
        """Best-effort audit trail of orders currently being reconciled,
        so a crash mid-poll leaves something an operator can check —
        this is NOT used to resume polling automatically on restart."""
        try:
            os.makedirs(os.path.dirname(PENDING_LIVE_ORDERS_PATH), exist_ok=True)
            try:
                with open(PENDING_LIVE_ORDERS_PATH) as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            data[result.order_id] = {
                "symbol": result.symbol,
                "side": result.side,
                "status": result.status,
                "amount": result.amount,
                "filled_amount": result.filled_amount,
                "trace_id": result.trace_id,
                "updated": _now(),
            }
            with open(PENDING_LIVE_ORDERS_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @staticmethod
    def _clear_pending_order(order_id: str) -> None:
        try:
            with open(PENDING_LIVE_ORDERS_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if order_id in data:
            data.pop(order_id, None)
            try:
                with open(PENDING_LIVE_ORDERS_PATH, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

    def get_pending_live_orders(self) -> dict[str, Any]:
        """Read-only view of orders still awaiting reconciliation
        confirmation (used by e.g. a /pendingorders diagnostic)."""
        try:
            with open(PENDING_LIVE_ORDERS_PATH) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _handle_live_protection(self, request: "OrderRequest", result: OrderResult) -> None:
        """Best-effort: after a LIVE fill, either create protection
        (BUY) or clean up now-orphaned protection (a SELL that leaves
        the position flat).

        Never raises — a protection failure must not look like the
        entry/exit order itself failed (the trade already happened).
        Failures ARE persisted to data/live_protections.json by
        ProtectionManager itself and surfaced via
        get_protection_status(), never silently dropped.
        """
        try:
            from scripts.protection_manager import (  # noqa: PLC0415
                ProtectionError,
                ProtectionManager,
            )
            from scripts.live_position_sync import (  # noqa: PLC0415
                LivePositionSync,
                MissingEntryPriceError,
            )
        except Exception:
            return

        side = (request.side or "").upper()
        symbol = request.symbol
        quote = getattr(self._config, "quote_currency", "USDT")
        pm = ProtectionManager(self._exchange, self._config)

        if side == "BUY":
            if not getattr(self._config, "auto_protect", False):
                return
            try:
                syncer = LivePositionSync(self._exchange, quote_currency=quote)
                positions = syncer.sync_positions([symbol])
                position = next((p for p in positions if p["symbol"] == symbol), None)
                if position is None:
                    return
                pm.create_protection(
                    position,
                    entry_order_id=result.order_id,
                    stop_price=request.stop_loss,
                    take_profit_price=request.take_profit,
                )
            except (ProtectionError, MissingEntryPriceError):
                _log.warning(
                    "Protection creation failed for %s — "
                    "position is UNPROTECTED (%s)",
                    symbol, request.side,
                )
            except Exception as exc:
                _log.warning(
                    "Protection creation failed for %s: %s — "
                    "position is UNPROTECTED",
                    symbol, exc,
                )

        elif side == "SELL":
            try:
                syncer = LivePositionSync(self._exchange, quote_currency=quote)
                remaining = syncer.sync_positions([symbol])
                still_held = any(
                    p["symbol"] == symbol and (p.get("quantity") or 0) > 1e-8
                    for p in remaining
                )
                if not still_held:
                    pm.cancel_protection(symbol, reason="position closed")
            except Exception:
                pass

    def get_protection_status(self, symbol: str) -> Optional[dict[str, Any]]:
        """Read-only: current protection record for a symbol (None if
        untracked). Used by /buy, /sell, /positions to tell the operator
        whether a live position actually has SL/TP attached."""
        if self._engine.mode != "LIVE":
            return None
        try:
            from scripts.protection_manager import ProtectionManager  # noqa: PLC0415
            return ProtectionManager(self._exchange, self._config).get_protection(symbol)
        except Exception:
            return None

    def reconcile_all_protections(self) -> dict[str, Any]:
        """Poll every ACTIVE protection record and cancel the sibling
        leg wherever one side has filled. NOT scheduled automatically by
        this method — call it periodically (scheduler job or a manual
        command) for the synthetic-OCO behavior to actually hold."""
        if self._engine.mode != "LIVE":
            return {}
        try:
            from scripts.protection_manager import ProtectionManager  # noqa: PLC0415
            return ProtectionManager(self._exchange, self._config).reconcile_all()
        except Exception:
            return {}

    def find_unprotected_live_positions(self) -> list[dict[str, Any]]:
        """Startup-recovery read: real LIVE positions that currently have
        NO active protection record. Detection only — never auto-creates
        orders, see ProtectionManager.find_unprotected_positions()."""
        if self._engine.mode != "LIVE":
            return []
        try:
            from scripts.protection_manager import ProtectionManager  # noqa: PLC0415
            from scripts.live_position_sync import LivePositionSync  # noqa: PLC0415
            quote = getattr(self._config, "quote_currency", "USDT")
            syncer = LivePositionSync(self._exchange, quote_currency=quote)
            positions = syncer.sync_all_positions()
            return ProtectionManager(self._exchange, self._config).find_unprotected_positions(
                positions,
            )
        except Exception:
            return []

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

    def live_readiness_report(self) -> dict[str, Any]:
        """Read-only diagnostic — used by /livecheck and /golive. Never
        arms anything by itself."""
        return self._engine.live_readiness_report()

    def arm_live(self, chat_id: str) -> dict[str, Any]:
        """Actually flip the LIVE switch and persist an audit record.

        Order matters here, deliberately:

            1. re-validate readiness (fresh balance/connection/permission
               check) — conditions can change in the seconds between
               /golive and CONFIRM LIVE (API key revoked, balance
               changed, permission changed, etc.)
            2. only THEN call enable_live()
            3. write the audit record last

        If step 1 fails, we never reach enable_live() — raises
        ``LiveArmError`` instead. If the audit write in step 3 fails,
        the switch has already been flipped correctly (callers should
        still treat trading as armed; the write failure only affects
        the on-disk audit trail, not the live in-memory state).

        Callers MUST have already obtained explicit operator
        confirmation (see telegram/commands/live.py) — this method
        itself does not ask for confirmation again, only validates.
        """
        report = self._engine.live_readiness_report()
        if not report.get("ready"):
            raise LiveArmError(
                "Readiness check failed at arm time: "
                + "; ".join(report.get("reasons") or ["unknown reason"])
            )

        self._engine.enable_live()
        record = {
            "armed": True,
            "time": _now(),
            "exchange": self._exchange.name,
            "confirmed_by_chat": str(chat_id),
        }
        self._write_live_armed_state(record)
        return record

    def disarm_live(self, reason: str = "manual") -> dict[str, Any]:
        self._engine.disable_live()
        record = {"armed": False, "time": _now(), "reason": reason}
        self._write_live_armed_state(record)
        return record

    @staticmethod
    def _write_live_armed_state(record: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(LIVE_ARMED_PATH), exist_ok=True)
            with open(LIVE_ARMED_PATH, "w") as f:
                json.dump(record, f, indent=2)
        except Exception:
            pass  # best-effort audit trail only; arming itself already happened

    @staticmethod
    def read_live_armed_state() -> dict[str, Any]:
        """Read the last-persisted arm/disarm record, if any.

        IMPORTANT: this is an audit record only. Nothing in this class
        (or anywhere at startup) may use this file's ``armed: true`` to
        automatically call ``enable_live()`` — every process restart
        requires a fresh /golive + CONFIRM LIVE from the operator.
        """
        try:
            with open(LIVE_ARMED_PATH) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

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
            fee = float(result.get("fee", 0.0) or 0.0)
        else:
            sym = result.symbol
            side = result.side
            amt = result.filled_amount or result.amount
            price = result.filled_price or result.price
            cost = result.cost or amt * price
            status = result.status
            pnl = getattr(result, 'net_pnl', 0.0)
            fee = float(getattr(result, 'fee', 0.0) or 0.0)

        if status not in ("FILLED", "EXECUTED"):
            return

        _sync_paper_files(sym, side.upper(), amt, price, cost, pnl, fee=fee)

    def sync_position(self, result: Any) -> None:
        """Mode-aware position sync — call this after a successful order
        instead of choosing between sync_paper_state()/sync_live_position()
        yourself. PAPER writes to data/positions.json (unchanged, existing
        behavior); LIVE re-derives the real position from the exchange
        and writes data/live_positions.json — separate files, separate
        worlds, so PAPER's existing pipeline is untouched.
        """
        if self._engine.mode == "LIVE":
            self.sync_live_position(result)
        else:
            self.sync_paper_state(result)

    def sync_live_position(self, result: Any) -> None:
        """Re-derive the REAL position for this order's symbol from the
        exchange (balance + trade history) and update
        data/live_positions.json.

        Deliberately re-fetches from the exchange rather than trusting
        the OrderResult's filled_amount/filled_price alone — the
        exchange is the source of truth; this is a cache refresh
        triggered by our own trade, not the record itself.
        """
        if self._engine.mode != "LIVE":
            return

        symbol = result.symbol if hasattr(result, "symbol") else result.get("symbol")
        if not symbol:
            return

        try:
            from scripts.live_position_sync import (  # noqa: PLC0415
                LivePositionSync,
                merge_live_positions,
            )
            syncer = LivePositionSync(
                self._exchange,
                quote_currency=getattr(self._config, "quote_currency", "USDT"),
            )
            fresh = syncer.sync_positions([symbol])
            merge_live_positions(fresh, synced_symbols=[symbol])
        except Exception:
            # Best-effort: a failed sync must NOT crash the trade flow —
            # the order itself already happened. /positions triggers its
            # own fresh sync on demand, so this only means the cache
            # stays stale until the next successful sync, not that the
            # trade is lost.
            pass

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
        qty = plan.get("quantity")
        if qty is not None and qty > 0:
            amount = qty
        else:
            usdt = plan.get("position_size_usdt", 0.0)
            ep = plan.get("entry_price", 1.0)
            amount = usdt / max(ep, 0.0001)
        return OrderRequest(
            trace_id=str(uuid.uuid4()),
            symbol=plan.get("symbol", ""),
            side="BUY",
            type="MARKET",
            amount=amount,
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

        # All retries exhausted — record exchange failure for cooldown
        if self._safeguard is not None:
            self._safeguard.record_exchange_failure()

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
            "open": "PENDING",
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


@paper_state_writes
def _sync_paper_files(
    symbol: str, side: str, amount: float, price: float, cost: float, pnl: float,
    fee: float = 0.0,
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
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(orders_path, orders_data, indent=2)
    except OSError:
        pass

    # --- Update paper_balance.json ---
    bal_path = f"{data_dir}/paper_balance.json"
    try:
        with open(bal_path) as f:
            pb = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pb = {
            "final_balance": float(os.getenv("ACCOUNT_BALANCE", "10000")),
            "final_equity": float(os.getenv("ACCOUNT_BALANCE", "10000")),
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        }

    if side == "BUY":
        pb["final_balance"] = round(pb.get("final_balance", float(os.getenv("ACCOUNT_BALANCE", "10000"))) - cost, 2)
    elif side == "SELL":
        proceeds = amount * price - fee
        # Close the position in the AUTHORITATIVE state files so the
        # next pipeline cycle can't resurrect it: paper_state.json
        # (wallet balance + position + order) and positions.json
        # (Telegram views). Returns the REAL realized PnL.
        pnl = _close_paper_position_on_sell(symbol, amount, price, proceeds)
        # Keep the order record consistent with the realized PnL.
        if orders_data.get("orders"):
            orders_data["orders"][-1]["net_pnl"] = round(pnl, 2)
        pb["final_balance"] = round(pb.get("final_balance", float(os.getenv("ACCOUNT_BALANCE", "10000"))) + proceeds, 2)
        pb["total_trades"] = pb.get("total_trades", 0) + 1
        if pnl > 0:
            pb["winning_trades"] = pb.get("winning_trades", 0) + 1
        else:
            pb["losing_trades"] = pb.get("losing_trades", 0) + 1
        total = pb.get("total_trades", 0)
        pb["win_rate"] = round(pb.get("winning_trades", 0) / total * 100, 2) if total else 0.0
        pb["realized_pnl"] = round(pb.get("realized_pnl", 0.0) + pnl, 2)

    # Use canonical function for ALL derived accounting metrics
    from scripts.metrics_manager import MetricsManager  # noqa: PLC0415

    pos_path = f"{data_dir}/positions.json"
    open_positions: list[dict[str, Any]] = []
    try:
        with open(pos_path) as f:
            pos_data = json.load(f)
        open_positions = [
            p for p in (pos_data.get("positions", []) if pos_data else [])
            if p.get("status") == "OPEN"
        ]
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    snapshot = MetricsManager.compute_snapshot(
        cash=pb.get("final_balance", 0.0),
        realized_pnl=pb.get("realized_pnl", 0.0),
        initial_balance=pb.get("initial_balance", float(os.getenv("ACCOUNT_BALANCE", "10000"))),
        open_positions=open_positions,
        total_trades=pb.get("total_trades", 0),
        winning_trades=pb.get("winning_trades", 0),
        losing_trades=pb.get("losing_trades", 0),
        win_rate=pb.get("win_rate", 0.0),
        profit_factor=pb.get("profit_factor", 0.0),
        gross_profit=pb.get("gross_profit", 0.0),
        gross_loss=pb.get("gross_loss", 0.0),
    )
    pb["final_equity"] = round(snapshot.equity, 2)
    pb["total_return_pct"] = round(snapshot.total_return_pct, 2)
    pb["unrealized_pnl"] = round(snapshot.unrealized_pnl, 2)
    pb["net_pnl"] = round(snapshot.net_pnl, 2)

    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(bal_path, pb, indent=2)
    except OSError:
        pass


@paper_state_writes
def _close_paper_position_on_sell(
    symbol: str,
    amount: float,
    price: float,
    proceeds: float,
) -> float:
    """Close an OPEN paper position after a manual /sell fills.

    ``paper_state.json`` is the authoritative state the pipeline re-derives
    everything from.  A manual SELL that only updated ``paper_balance.json``
    used to be silently reverted by the next pipeline cycle: the position
    was reloaded as still-OPEN and the cash figure reverted to the
    pre-sale balance.  This patches ``paper_state.json`` (wallet balance +
    CLOSED position + SELL order) and ``positions.json`` so the closure is
    durable and immediately visible to Telegram commands.

    Returns the realized PnL computed against the position's cost basis
    (0.0 when no matching OPEN position is found).
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    now_ts = datetime.now(timezone.utc).isoformat()
    data_dir = "data"

    state_path = f"{data_dir}/paper_state.json"
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = None

    pnl = 0.0
    vp = (state.get("positions") or {}).get(symbol) if state is not None else None

    if vp is not None and vp.get("status") == "OPEN":
        qty_total = float(vp.get("quantity", 0) or 0)
        cost_basis = float(vp.get("cost_basis", 0) or 0)
        cost_part = cost_basis * (amount / qty_total) if qty_total > 0 else 0.0
        pnl = proceeds - cost_part

        # Credit the wallet so the next pipeline cycle sees the proceeds.
        state["balance"] = round(float(state.get("balance", 0.0)) + proceeds, 2)

        vp["status"] = "CLOSED"
        vp["remaining_qty"] = 0.0
        vp["unrealized_pnl"] = 0.0
        vp["realized_pnl"] = round(pnl, 2)
        vp["total_pnl"] = round(pnl, 2)
        vp["current_price"] = price
        vp["closure_notified"] = True

        entry_price = float(vp.get("entry_price", 0) or 0)
        net_pnl_pct = round((pnl / cost_part * 100), 2) if cost_part > 0 else 0.0

        state.setdefault("orders", []).append({
            "id": f"manual-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": amount,
            "filled_quantity": amount,
            "entry_price": entry_price,
            "fill_price": price,
            "slippage": 0.0,
            "entry_fee": 0.0,
            "exit_price": price,
            "exit_fee": 0.0,
            "total_cost": round(cost_part, 2),
            "total_proceeds": round(proceeds, 2),
            "net_pnl": round(pnl, 2),
            "net_pnl_pct": net_pnl_pct,
            "status": "CLOSED",
            "created_at": now_ts,
            "filled_at": now_ts,
            "closed_at": now_ts,
            "exit_reason": "manual",
        })

        try:
            from scripts.paper_state_lock import atomic_write_json as _awj
            _awj(state_path, state, indent=2, default=str)
        except OSError:
            pass

    # Close the position in positions.json too so /status and /positions
    # reflect the closure immediately — this must run regardless of
    # whether paper_state.json had a matching OPEN entry above. Relying
    # on that guard used to let an out-of-sync paper_state.json silently
    # skip this block entirely, leaving positions.json (what Telegram
    # actually reads) showing a position that had already been sold.
    from scripts.position_status import is_open  # noqa: PLC0415
    pos_path = f"{data_dir}/positions.json"
    try:
        with open(pos_path) as f:
            pos_data = json.load(f)
        for p in pos_data.get("positions", []):
            if p.get("symbol") == symbol and is_open(p.get("status")):
                if vp is None:
                    # paper_state.json didn't have this position — fall
                    # back to positions.json's own cost basis so PnL is
                    # still reported instead of a misleading 0.0.
                    fallback_cost_basis = float(p.get("cost_basis", 0.0) or 0.0)
                    if fallback_cost_basis <= 0:
                        fallback_cost_basis = float(p.get("entry_price", 0.0) or 0.0) * amount
                    pnl = proceeds - fallback_cost_basis
                p["status"] = "CLOSED"
                p["remaining_qty"] = 0.0
                p["unrealized_pnl"] = 0.0
                p["realized_pnl"] = round(pnl, 2)
                p["total_pnl"] = round(pnl, 2)
                p["closed_at"] = now_ts
                break
        pos_data["total_positions"] = len(pos_data.get("positions", []))
        pos_data["active_count"] = sum(
            1 for p in pos_data.get("positions", [])
            if is_open(p.get("status"))
        )
        pos_data["closed_count"] = sum(
            1 for p in pos_data.get("positions", [])
            if not is_open(p.get("status"))
        )
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(pos_path, pos_data, indent=2, default=str)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return pnl
