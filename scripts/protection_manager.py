"""Protective orders (Stop Loss / Take Profit) for LIVE positions.

Deliberately a SEPARATE module from execution_engine.py / order_manager.py
by design:
    - manages ONLY protective (exit) orders for positions that already
      exist and are already FILLED
    - never touches entry orders, never runs in PAPER mode
    - execution_engine.py is not modified for this — OrderManager wires
      this in as an extra step, not the engine itself

============================================================================
IMPORTANT — this implements "paired protective orders" (a synthetic
OCO), NOT an exchange-native linked OCO order.
============================================================================
A true native OCO (e.g. Binance's OCO order type) is matched atomically
on the exchange's own matching engine — when one leg fills, the
exchange itself cancels the other, with zero gap.

Implementing that correctly requires exchange-specific raw API
parameters that differ across ccxt versions, which could not be
verified with confidence in this environment (no access to live,
version-matched ccxt docs). Getting that wrong with real money on the
line is worse than being upfront about a safer, verifiable fallback.

Here, ``ProtectionManager`` submits the stop-loss and take-profit as
TWO ORDINARY, INDEPENDENT sell orders via the same ``create_order()``
path already used (and tested) elsewhere in this codebase, and is
itself responsible for cancelling the sibling order once one leg
fills — see ``reconcile_protection()``. This class does NOT loop
itself — polling is done by ``scripts/protection_scheduler.py``
(started automatically from ``main.py`` in LIVE mode, every
``config.protection_reconcile_interval_seconds``), with
``/protectioncheck`` available as an additional on-demand trigger.

KNOWN GAP: if price moves through both the stop and target levels
before the next reconciliation poll runs, both legs could theoretically
fill before the sibling gets cancelled. This is disclosed, not hidden.
Keep the reconciliation poll interval short if you rely on this.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from scripts.exchange_manager import ExchangeManager
from scripts.exchange_providers import is_market_tradeable
from scripts.exit_gate import exit_guard
from scripts.live_position_sync import require_entry_price

LIVE_PROTECTIONS_PATH = "data/live_protections.json"

# The exact order-type string a stop-loss leg needs varies by exchange
# and ccxt version. This is Binance's raw API convention (STOP_LOSS_LIMIT),
# which ccxt's Binance implementation is known to pass through — but
# VERIFY this against your exchange/ccxt version before relying on it
# live. Change here if it doesn't match reality for your setup.
STOP_LOSS_ORDER_TYPE = "STOP_LOSS_LIMIT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProtectionError(Exception):
    """Raised when protective orders could not be created/managed.

    Callers MUST treat this as "position is unprotected" — never
    silently continue as if protection succeeded.
    """


class ProtectionManager:
    """Creates and reconciles paired SL/TP orders for LIVE positions.

    Every method here operates on a ``position`` dict shaped like
    ``LivePositionSync.sync_positions()``'s output — i.e. real exchange
    data (quantity, entry_price). Never pass it data derived from
    positions.json / paper state.
    """

    def __init__(self, exchange: ExchangeManager, config: Any) -> None:
        self._exchange = exchange
        self._config = config

    # ------------------------------------------------------------------
    #  Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _load_all() -> dict[str, Any]:
        try:
            with open(LIVE_PROTECTIONS_PATH) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save_all(data: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(LIVE_PROTECTIONS_PATH), exist_ok=True)
            with open(LIVE_PROTECTIONS_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # best-effort audit trail; in-flight orders already exist regardless

    def _save_one(self, symbol: str, record: dict[str, Any]) -> None:
        data = self._load_all()
        data[symbol] = record
        self._save_all(data)

    def get_protection(self, symbol: str) -> Optional[dict[str, Any]]:
        return self._load_all().get(symbol)

    def get_all_protections(self) -> dict[str, Any]:
        return self._load_all()

    # ------------------------------------------------------------------
    #  Guards
    # ------------------------------------------------------------------

    def _guard(self, position: dict[str, Any]) -> tuple[str, float, float]:
        """Validate everything BEFORE any order is submitted.

        Returns (symbol, quantity, entry_price) if all guards pass.
        Raises ProtectionError / MissingEntryPriceError otherwise —
        never proceeds partially.
        """
        symbol = position.get("symbol")
        if not symbol:
            raise ProtectionError("Cannot create protection: position has no symbol.")

        quantity = position.get("quantity")
        if not quantity or quantity <= 0:
            raise ProtectionError(
                f"Cannot create protection for {symbol}: invalid quantity {quantity}.",
            )

        entry_price = require_entry_price(position)  # raises MissingEntryPriceError

        try:
            provider = self._exchange.get_provider()
        except Exception as exc:
            raise ProtectionError(
                f"Cannot create protection for {symbol}: exchange unreachable — {exc}",
            ) from exc

        markets = provider.load_markets()
        if markets and symbol not in markets:
            raise ProtectionError(
                f"Cannot create protection for {symbol}: not a valid market on "
                f"{provider.name}.",
            )
        if not is_market_tradeable(provider, symbol):
            raise ProtectionError(
                f"Cannot create protection for {symbol}: market is under "
                f"maintenance/suspended on {provider.name}.",
            )

        return symbol, quantity, entry_price

    # ------------------------------------------------------------------
    #  Create
    # ------------------------------------------------------------------

    def create_protection(
        self,
        position: dict[str, Any],
        entry_order_id: str = "",
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> dict[str, Any]:
        """Submit paired SL/TP sell orders for an existing LIVE long position.

        Serialized on the same per-symbol lock used by every other exit
        path (see ``scripts.exit_gate``) so protection creation can
        never race a concurrent market exit for the same symbol.
        """
        with exit_guard(position.get("symbol", "")):
            return self._create_protection(
                position,
                entry_order_id=entry_order_id,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )

    def _create_protection(
        self,
        position: dict[str, Any],
        entry_order_id: str = "",
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> dict[str, Any]:
        """Submit paired SL/TP sell orders for an existing LIVE long position.

        ``position`` MUST come from ``LivePositionSync`` (real exchange
        data), never from positions.json / paper state.

        If ``stop_price``/``take_profit_price`` aren't given explicitly
        (e.g. from a trade plan), they default to
        ``config.default_stop_pct`` / ``config.default_take_profit_pct``
        around the position's real entry_price.

        Raises ``ProtectionError`` (or ``MissingEntryPriceError``, from
        live_position_sync) if ANY guard fails — in that case NO order
        is submitted at all. If the stop-loss leg succeeds but the
        take-profit leg then fails, the position is left with a
        stop-loss ONLY, recorded with status ``PARTIAL_ERROR`` and
        re-raised — never silently treated as fully protected.
        """
        symbol, quantity, entry_price = self._guard(position)
        provider = self._exchange.get_provider()

        if stop_price is None:
            stop_pct = getattr(self._config, "default_stop_pct", 3.0)
            stop_price = entry_price * (1 - stop_pct / 100.0)
        if take_profit_price is None:
            tp_pct = getattr(self._config, "default_take_profit_pct", 6.0)
            take_profit_price = entry_price * (1 + tp_pct / 100.0)

        quantity_p = provider.amount_to_precision(symbol, quantity)
        stop_price_p = provider.price_to_precision(symbol, stop_price)
        take_profit_price_p = provider.price_to_precision(symbol, take_profit_price)

        record: dict[str, Any] = {
            "symbol": symbol,
            "entry_order_id": entry_order_id,
            "quantity": quantity_p,
            "entry_price": entry_price,
            "stop_price": stop_price_p,
            "take_profit_price": take_profit_price_p,
            "stop_order_id": None,
            "take_profit_order_id": None,
            "status": "PENDING",
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }

        ex = provider._get_exchange()

        # Leg 1: stop-loss. If this fails, nothing has been submitted
        # yet — safe to just report the error.
        try:
            id_params = provider.client_order_id_params(
                f"sl{entry_order_id}"[:34] if entry_order_id else "",
            )
            stop_order = ex.create_order(
                symbol=symbol,
                type=STOP_LOSS_ORDER_TYPE,
                side="sell",
                amount=quantity_p,
                price=stop_price_p,
                params={"stopPrice": stop_price_p, **id_params},
            )
            record["stop_order_id"] = str(stop_order.get("id", ""))
        except Exception as exc:
            record["status"] = "ERROR"
            record["error"] = f"stop-loss order failed: {exc}"
            self._save_one(symbol, record)
            raise ProtectionError(record["error"]) from exc

        # Leg 2: take-profit. If THIS fails, the stop-loss is already
        # live on the exchange — do not pretend nothing happened.
        try:
            id_params = provider.client_order_id_params(
                f"tp{entry_order_id}"[:34] if entry_order_id else "",
            )
            tp_order = ex.create_order(
                symbol=symbol,
                type="limit",
                side="sell",
                amount=quantity_p,
                price=take_profit_price_p,
                params=id_params,
            )
            record["take_profit_order_id"] = str(tp_order.get("id", ""))
        except Exception as exc:
            record["status"] = "PARTIAL_ERROR"
            record["error"] = (
                f"take-profit order failed (stop-loss IS active, order_id="
                f"{record['stop_order_id']}): {exc}"
            )
            record["updated_at"] = _now()
            self._save_one(symbol, record)
            raise ProtectionError(record["error"]) from exc

        record["status"] = "ACTIVE"
        record["updated_at"] = _now()
        self._save_one(symbol, record)
        return record

    # ------------------------------------------------------------------
    #  Reconcile (poll — call this periodically, e.g. from the scheduler
    #  or a manual /protectioncheck command)
    # ------------------------------------------------------------------

    def reconcile_protection(self, symbol: str) -> Optional[dict[str, Any]]:
        """Check both legs for ``symbol``; if one filled, cancel the
        other (this bot IS the OCO logic — see module docstring).

        Serialized on the same per-symbol lock as every other exit path.
        """
        with exit_guard(symbol):
            return self._reconcile_protection(symbol)

    def _reconcile_protection(self, symbol: str) -> Optional[dict[str, Any]]:
        """Check both legs for ``symbol``; if one filled, cancel the
        other (this bot IS the OCO logic — see module docstring).

        Returns the updated record, or None if there's nothing tracked
        for this symbol.
        """
        record = self.get_protection(symbol)
        if not record or record.get("status") != "ACTIVE":
            return record

        provider = self._exchange.get_provider()
        ex = provider._get_exchange()

        stop_id = record.get("stop_order_id")
        tp_id = record.get("take_profit_order_id")

        stop_status = self._safe_fetch_status(ex, stop_id, symbol)
        tp_status = self._safe_fetch_status(ex, tp_id, symbol)

        stop_filled = stop_status in ("closed", "filled")
        tp_filled = tp_status in ("closed", "filled")

        if stop_filled and tp_filled:
            # Both filled — the exact race condition disclosed in the
            # module docstring. Flag loudly instead of picking one.
            record["status"] = "ERROR"
            record["error"] = (
                "Both stop-loss and take-profit filled — the reconciliation "
                "poll was too slow to cancel the sibling in time. Manual "
                "review required."
            )
            record["updated_at"] = _now()
            self._save_one(symbol, record)
            return record

        if stop_filled:
            self._safe_cancel(ex, tp_id, symbol)
            record["status"] = "STOP_FILLED"
            record["updated_at"] = _now()
            self._save_one(symbol, record)
            return record

        if tp_filled:
            self._safe_cancel(ex, stop_id, symbol)
            record["status"] = "TP_FILLED"
            record["updated_at"] = _now()
            self._save_one(symbol, record)
            return record

        return record  # still active, nothing changed

    @staticmethod
    def _safe_fetch_status(ex: Any, order_id: Optional[str], symbol: str) -> Optional[str]:
        if not order_id:
            return None
        try:
            order = ex.fetch_order(order_id, symbol)
            return str(order.get("status", "")).lower()
        except Exception:
            return None  # can't confirm right now — treated as "still open"

    @staticmethod
    def _safe_cancel(ex: Any, order_id: Optional[str], symbol: str) -> None:
        if not order_id:
            return
        try:
            ex.cancel_order(order_id, symbol)
        except Exception:
            pass  # best-effort — if it already filled/cancelled, cancel fails harmlessly

    def reconcile_all(self) -> dict[str, Any]:
        """Reconcile every ACTIVE protection record. Meant to be called
        periodically — this module does not schedule itself."""
        results = {}
        for symbol, record in self._load_all().items():
            if record.get("status") == "ACTIVE":
                results[symbol] = self.reconcile_protection(symbol)
        return results

    # ------------------------------------------------------------------
    #  Cancel (e.g. position fully closed — protection orders are now
    #  orphaned and would sell something the account no longer holds)
    # ------------------------------------------------------------------

    def cancel_protection(self, symbol: str, reason: str = "manual") -> Optional[dict[str, Any]]:
        with exit_guard(symbol):
            return self._cancel_protection(symbol, reason)

    def _cancel_protection(self, symbol: str, reason: str = "manual") -> Optional[dict[str, Any]]:
        record = self.get_protection(symbol)
        if not record:
            return None

        provider = self._exchange.get_provider()
        ex = provider._get_exchange()
        self._safe_cancel(ex, record.get("stop_order_id"), symbol)
        self._safe_cancel(ex, record.get("take_profit_order_id"), symbol)

        record["status"] = "CANCELLED"
        record["error"] = f"cancelled: {reason}"
        record["updated_at"] = _now()
        self._save_one(symbol, record)
        return record

    # ------------------------------------------------------------------
    #  Startup recovery — DETECT ONLY, never auto-recreate silently.
    #  Real-money order placement always needs an explicit trigger.
    # ------------------------------------------------------------------

    def find_unprotected_positions(
        self, live_positions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compare real LIVE positions against tracked protection state.

        Returns positions that have real holdings but no ACTIVE
        protection record — e.g. after a restart where protection
        orders were placed but the record wasn't (crash mid-write), or
        a position opened without auto_protect enabled, or a position
        whose protection legs both got cancelled/errored out.

        Deliberately does NOT create anything — see module docstring:
        this is surfaced to the operator (main.py logs it, or a
        Telegram command) who decides whether/how to protect it,
        instead of the bot guessing stop/target levels on its own.
        """
        tracked = self._load_all()
        unprotected = []
        for pos in live_positions:
            symbol = pos.get("symbol")
            record = tracked.get(symbol)
            if record is None or record.get("status") not in ("ACTIVE", "PENDING"):
                unprotected.append(pos)
        return unprotected
