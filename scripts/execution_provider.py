"""ExecutionProvider — abstraction layer for order execution.

Architecture::

    Unified Pipeline (shared business logic)
           │
           ▼
    ExecutionProvider (abstract)
        ├── PaperExecutionProvider  (simulated orders)
        └── LiveExecutionProvider   (CCXT exchange orders)

All TP/SL logic, position management, portfolio updates use the SAME
code path. Only the bottom-level order submission differs.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES


# ======================================================================
#  Pipeline Event Logging
# ======================================================================

EVENT_LOG_PATH = "data/execution_events.jsonl"


class PipelineEvent:
    """Structured event emitted at each stage of the execution pipeline.

    Events::

        SIGNAL_GENERATED     — signal received from strategy
        ORDER_VALIDATED      — order passed all validations
        ORDER_SUBMITTED      — order sent to executor
        ORDER_FILLED         — order confirmed filled
        POSITION_OPENED      — position recorded after fill
        TP_TRIGGERED         — take-profit level hit
        SL_TRIGGERED         — stop-loss level hit
        EXIT_SUBMITTED       — exit order submitted
        POSITION_CLOSED      — position fully closed
        ORDER_REJECTED       — order rejected by validation or executor
    """

    def __init__(
        self, event: str, symbol: str, **kwargs: Any,
    ) -> None:
        self.event = event
        self.symbol = symbol
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.data = kwargs

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            **self.data,
        }


_event_log: list[PipelineEvent] = []


def emit_event(event: PipelineEvent) -> None:
    """Record a pipeline event in memory and append to the JSONL file."""
    _event_log.append(event)
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
        with open(EVENT_LOG_PATH, "a") as f:
            f.write(
                json.dumps(event.to_dict()) + "\n"  # noqa: PNTANALYZER
            )
    except Exception:
        pass


def get_events(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent *limit* events."""
    return [e.to_dict() for e in _event_log[-limit:]]


def clear_events() -> None:
    _event_log.clear()


# ======================================================================
#  Shared Order Models
# ======================================================================


@dataclass
class OrderRequest:
    trace_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    side: str = "BUY"
    type: str = "MARKET"
    amount: float = 0.0
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())
        if not self.client_order_id:
            self.client_order_id = "zb" + self.trace_id.replace("-", "")[:34]


@dataclass
class OrderResult:
    order_id: str = ""
    trace_id: str = ""
    execution_id: str = ""
    status: str = "PENDING"
    symbol: str = ""
    side: str = ""
    type: str = ""
    amount: float = 0.0
    filled_amount: float = 0.0
    filled_price: float = 0.0
    fee: float = 0.0
    cost: float = 0.0
    error: Optional[str] = None
    latency_ms: float = 0.0
    retries: int = 0
    executor: str = ""
    exchange: str = ""
    mode: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def rejected(
        cls, request: OrderRequest, reason: str, executor: str = "",
    ) -> OrderResult:
        return cls(
            trace_id=request.trace_id,
            execution_id=str(uuid.uuid4()),
            status="REJECTED",
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            amount=request.amount,
            error=reason,
            executor=executor,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def failed(
        cls, request: OrderRequest, reason: str, executor: str = "",
    ) -> OrderResult:
        return cls(
            trace_id=request.trace_id,
            execution_id=str(uuid.uuid4()),
            status="FAILED",
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            amount=request.amount,
            error=reason,
            executor=executor,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


# ======================================================================
#  ExecutionProvider — Abstract Interface
# ======================================================================


class ExecutionProvider(ABC):
    """Abstract execution provider.

    Subclasses implement only the primitive operations:
      - execute_buy
      - execute_sell
      - get_balance
      - get_current_price
      - get_exchange_name

    All higher-level orchestration (validation, TP/SL checking,
    position management) is shared in the unified pipeline.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def mode(self) -> str: ...

    @abstractmethod
    def execute_buy(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def execute_sell(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def get_balance(self) -> float: ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> Optional[float]: ...

    @abstractmethod
    def get_exchange_name(self) -> str: ...

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        return price


# ======================================================================
#  PaperExecutionProvider
# ======================================================================


PAPER_INITIAL_BALANCE = 10_000.0
PAPER_TAKER_FEE = 0.001
PAPER_SLIPPAGE_BPS = 3
PAPER_STATE_PATH = "data/paper_state.json"
PAPER_BALANCE_PATH = "data/paper_balance.json"


@dataclass
class PaperPosition:
    symbol: str
    order_id: str
    quantity: float
    remaining_qty: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    cost_basis: float
    status: str
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    stop_loss: float = 0.0
    opened_at: str = ""
    signal_time: str = ""
    closure_notified: bool = False


class PaperBalance:
    """Virtual wallet for paper trading."""

    def __init__(self) -> None:
        self.initial = PAPER_INITIAL_BALANCE
        self.balance = PAPER_INITIAL_BALANCE

    def load(self) -> None:
        try:
            import json
            with open(PAPER_BALANCE_PATH) as f:
                data = json.load(f)
            self.balance = data.get("final_balance", PAPER_INITIAL_BALANCE)
            self.initial = data.get("initial_balance", PAPER_INITIAL_BALANCE)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            import json
            with open(PAPER_STATE_PATH) as f:
                state = json.load(f)
            bal = state.get("balance")
            if bal is not None:
                self.balance = bal
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    @property
    def free_balance(self) -> float:
        return max(0.0, self.balance)

    def deduct(self, amount: float) -> bool:
        if amount > self.balance:
            return False
        self.balance -= amount
        return True

    def add(self, amount: float) -> None:
        self.balance += amount

    def save(self) -> None:
        import json
        os.makedirs("data", exist_ok=True)
        data = {
            "initial_balance": self.initial,
            "final_balance": round(self.balance, 2),
            "final_equity": round(self.balance, 2),
        }
        with open(PAPER_BALANCE_PATH, "w") as f:
            json.dump(data, f, indent=2)


def _paper_buy(price: float, qty: float) -> dict[str, float]:
    slippage = price * (PAPER_SLIPPAGE_BPS / 10000.0)
    fill_price = price + slippage
    gross = fill_price * qty
    fee = gross * PAPER_TAKER_FEE
    return {"fill_price": fill_price, "fee": fee, "total_cost": gross + fee, "slippage": slippage}


def _paper_sell(price: float, qty: float) -> dict[str, float]:
    slippage = price * (PAPER_SLIPPAGE_BPS / 10000.0)
    fill_price = price - slippage
    gross = fill_price * qty
    fee = gross * PAPER_TAKER_FEE
    return {"fill_price": fill_price, "fee": fee, "total_proceeds": gross - fee, "slippage": slippage}


class PaperExecutionProvider(ExecutionProvider):
    """Simulates orders with fee and slippage."""

    name = "paper"
    mode = "PAPER"

    def __init__(self) -> None:
        self.balance = PaperBalance()
        self.balance.load()
        self.positions: dict[str, PaperPosition] = {}
        self._load_positions()

    def _load_positions(self) -> None:
        import json
        try:
            with open(PAPER_STATE_PATH) as f:
                state = json.load(f)
            for sym, vp in state.get("positions", {}).items():
                self.positions[sym] = PaperPosition(
                    symbol=vp.get("symbol", sym),
                    order_id=vp.get("order_id", ""),
                    quantity=vp.get("quantity", 0),
                    remaining_qty=vp.get("remaining_qty", 0),
                    entry_price=vp.get("entry_price", 0),
                    current_price=vp.get("current_price", 0),
                    unrealized_pnl=vp.get("unrealized_pnl", 0),
                    realized_pnl=vp.get("realized_pnl", 0),
                    total_pnl=vp.get("total_pnl", 0),
                    cost_basis=vp.get("cost_basis", 0),
                    status=vp.get("status", "OPEN"),
                    tp1=vp.get("tp1", 0),
                    tp2=vp.get("tp2", 0),
                    tp3=vp.get("tp3", 0),
                    stop_loss=vp.get("stop_loss", 0),
                    opened_at=vp.get("opened_at", ""),
                    signal_time=vp.get("signal_time", ""),
                    closure_notified=vp.get("closure_notified", False),
                )
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_positions(self) -> None:
        import json
        os.makedirs("data", exist_ok=True)
        state: dict[str, Any] = {
            "version": 1,
            "balance": self.balance.balance,
            "initial_balance": self.balance.initial,
            "margin_used": 0.0,
            "orders": [],
            "positions": {
                sym: asdict(vp) for sym, vp in self.positions.items()
            },
            "equity_history": [],
        }
        with open(PAPER_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def get_exchange_name(self) -> str:
        return os.getenv("EXCHANGE", "binance")

    def get_balance(self) -> float:
        return self.balance.free_balance

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            import ccxt
            ex = ccxt.binance({"enableRateLimit": True, "timeout": 15000})
            ticker = ex.fetch_ticker(symbol)
            return float(ticker.get("last", 0) or 0)
        except Exception:
            return None

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return _round_qty(amount)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return _round_qty(price)

    def execute_buy(self, request: OrderRequest) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        amount = request.amount

        price = request.price
        if price is None or price <= 0:
            cp = self.get_current_price(symbol)
            if cp and cp > 0:
                price = cp
            else:
                return OrderResult.rejected(
                    request, f"Cannot determine price for {symbol}", self.name,
                )

        qty = amount if amount > 0 else 0.0
        if qty <= 0:
            return OrderResult.rejected(request, f"Invalid amount: {qty}", self.name)

        result = _paper_buy(price, qty)
        fill_price = result["fill_price"]
        total_cost = result["total_cost"]

        if total_cost > self.balance.free_balance:
            return OrderResult.rejected(
                request,
                f"Insufficient balance: need {total_cost:.2f}, have {self.balance.free_balance:.2f}",
                self.name,
            )

        self.balance.deduct(total_cost)
        self.balance.save()

        order_id = "po_" + uuid.uuid4().hex[:12]
        elapsed = (time.time() - t0) * 1000

        # Record position
        self.positions[symbol] = PaperPosition(
            symbol=symbol,
            order_id=order_id,
            quantity=qty,
            remaining_qty=qty,
            entry_price=fill_price,
            current_price=price,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            total_pnl=0.0,
            cost_basis=total_cost,
            status="OPEN",
            tp1=request.metadata.get("tp1", 0) or request.take_profit or 0,
            tp2=request.metadata.get("tp2", 0),
            tp3=request.metadata.get("tp3", 0),
            stop_loss=request.metadata.get("stop_loss", 0) or request.stop_loss or 0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            signal_time=request.metadata.get("signal_time", ""),
        )
        self._save_positions()
        emit_event(PipelineEvent("POSITION_OPENED", symbol, order_id=order_id, price=fill_price, qty=qty))

        return OrderResult(
            order_id=order_id,
            trace_id=request.trace_id,
            execution_id=str(uuid.uuid4()),
            status="FILLED",
            symbol=symbol,
            side="BUY",
            type=request.type,
            amount=qty,
            filled_amount=qty,
            filled_price=round(fill_price, 8),
            fee=round(result["fee"], 8),
            cost=round(total_cost, 8),
            latency_ms=round(elapsed, 2),
            retries=0,
            executor=self.name,
            exchange=self.get_exchange_name(),
            mode="PAPER",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def execute_sell(self, request: OrderRequest) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        amount = request.amount

        price = request.price
        if price is None or price <= 0:
            cp = self.get_current_price(symbol)
            if cp and cp > 0:
                price = cp
            else:
                return OrderResult.rejected(
                    request, f"Cannot determine price for {symbol}", self.name,
                )

        qty = amount if amount > 0 else 0.0
        if qty <= 0:
            return OrderResult.rejected(request, f"Invalid amount: {qty}", self.name)

        result = _paper_sell(price, qty)
        fill_price = result["fill_price"]
        total_proceeds = result["total_proceeds"]

        self.balance.add(total_proceeds)
        self.balance.save()

        elapsed = (time.time() - t0) * 1000

        return OrderResult(
            order_id="po_" + uuid.uuid4().hex[:12],
            trace_id=request.trace_id,
            execution_id=str(uuid.uuid4()),
            status="FILLED",
            symbol=symbol,
            side="SELL",
            type=request.type,
            amount=qty,
            filled_amount=qty,
            filled_price=round(fill_price, 8),
            fee=round(result["fee"], 8),
            cost=round(total_proceeds, 8),
            latency_ms=round(elapsed, 2),
            retries=0,
            executor=self.name,
            exchange=self.get_exchange_name(),
            mode="PAPER",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def update_position_price(self, symbol: str, price: float) -> None:
        vp = self.positions.get(symbol)
        if vp is None or vp.status not in OPEN_STATUSES:
            return
        vp.current_price = price
        remaining_qty = vp.remaining_qty
        cost_remaining = vp.cost_basis * (remaining_qty / vp.quantity) if vp.quantity > 0 else 0
        vp.unrealized_pnl = round(price * remaining_qty - cost_remaining, 2)
        vp.total_pnl = round(vp.realized_pnl + vp.unrealized_pnl, 2)
        self._save_positions()


def _round_qty(qty: float) -> float:
    if qty <= 0:
        return 0.0
    if qty >= 1000:
        return round(qty, 2)
    if qty >= 1:
        return round(qty, 4)
    if qty >= 0.001:
        return round(qty, 6)
    return round(qty, 8)


# ======================================================================
#  LiveExecutionProvider
# ======================================================================


class LiveExecutionProvider(ExecutionProvider):
    """Executes orders on a real exchange via CCXT."""

    name = "live"
    mode = "LIVE"

    def __init__(self, exchange_manager: Any, config: Any) -> None:
        self._exchange = exchange_manager
        self._config = config

    def get_exchange_name(self) -> str:
        return getattr(self._exchange, "name", "binance")

    def get_balance(self) -> float:
        try:
            provider = self._exchange.get_provider()
            raw = provider.fetch_balance()
            quote = (getattr(self._config, "quote_currency", "") or "USDT").upper()
            free = None
            bucket = raw.get("free")
            if isinstance(bucket, dict) and quote in bucket:
                free = float(bucket[quote])
            if free is None:
                per = raw.get(quote)
                if isinstance(per, dict):
                    free = float(per.get("free", 0))
            return free if free is not None else 0.0
        except Exception:
            return 0.0

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            provider = self._exchange.get_provider()
            raw = provider.fetch_ticker(symbol)
            if isinstance(raw, dict):
                return float(raw.get("last", 0) or raw.get("ask", 0) or raw.get("bid", 0) or 0)
            if hasattr(raw, "last"):
                return float(raw.last or 0)
            return None
        except Exception:
            return None

    def get_asset_balance(self, symbol: str) -> float:
        """Return the FREE balance of the base asset (e.g. BOME in BOME/USDT).

        Used before every live SELL so we never ask the exchange to sell
        more than we actually hold. Internal position tracking (quantity /
        remaining_qty in positions.json) is derived from the trade plan at
        signal time, not from what actually got filled/kept after fees and
        exchange precision — so it can drift from the real wallet balance.
        """
        try:
            provider = self._exchange.get_provider()
            raw = provider.fetch_balance()
            base = symbol.split("/")[0].upper()
            free = None
            bucket = raw.get("free")
            if isinstance(bucket, dict) and base in bucket:
                free = float(bucket[base])
            if free is None:
                per = raw.get(base)
                if isinstance(per, dict):
                    free = float(per.get("free", 0))
            return free if free is not None else 0.0
        except Exception:
            return 0.0

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            provider = self._exchange.get_provider()
            return float(provider.amount_to_precision(symbol, amount))
        except Exception:
            return _round_qty(amount)

    def price_to_precision(self, symbol: str, price: float) -> float:
        try:
            provider = self._exchange.get_provider()
            return float(provider.price_to_precision(symbol, price))
        except Exception:
            return _round_qty(price)

    def execute_buy(self, request: OrderRequest) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        amount = request.amount

        amount_p = self.amount_to_precision(symbol, amount)
        if amount_p <= 0:
            return OrderResult.rejected(request, f"Invalid amount {amount} after precision", self.name)

        price = request.price
        if price is None or price <= 0:
            cp = self.get_current_price(symbol)
            if cp and cp > 0:
                price = cp
            else:
                return OrderResult.rejected(
                    request, f"Cannot determine price for {symbol}", self.name,
                )

        balance = self.get_balance()
        est_cost = amount_p * price
        if est_cost > balance:
            return OrderResult.rejected(
                request,
                f"Insufficient balance: need ~{est_cost:.2f}, have {balance:.2f}",
                self.name,
            )

        try:
            provider = self._exchange.get_provider()
            ex = provider._get_exchange()
            price_p = None
            id_params = provider.client_order_id_params(request.client_order_id)
            ccxt_order = ex.create_order(
                symbol=symbol,
                type="market",
                side="buy",
                amount=amount_p,
                price=price_p,
                params=id_params,
            )
            elapsed = (time.time() - t0) * 1000
            status = _map_live_status(ccxt_order, amount_p)
            return OrderResult(
                order_id=str(ccxt_order.get("id", "")),
                trace_id=request.trace_id,
                execution_id=str(uuid.uuid4()),
                status=status,
                symbol=symbol,
                side="BUY",
                type=request.type,
                amount=amount_p,
                filled_amount=float(ccxt_order.get("filled", 0)),
                filled_price=float(ccxt_order.get("average") or ccxt_order.get("price", price)),
                fee=float(ccxt_order.get("fee", {}).get("cost", 0) if isinstance(ccxt_order.get("fee"), dict) else 0),
                cost=float(ccxt_order.get("cost", 0)),
                latency_ms=round(elapsed, 2),
                retries=0,
                executor=self.name,
                exchange=self.get_exchange_name(),
                mode="LIVE",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            return OrderResult.failed(request, f"Live BUY error: {exc}", self.name)

    def execute_sell(self, request: OrderRequest) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        amount = request.amount

        amount_p = self.amount_to_precision(symbol, amount)
        if amount_p <= 0:
            return OrderResult.rejected(request, f"Invalid amount {amount} after precision", self.name)

        price = request.price
        if price is None or price <= 0:
            cp = self.get_current_price(symbol)
            if cp and cp > 0:
                price = cp
            else:
                return OrderResult.rejected(
                    request, f"Cannot determine price for {symbol}", self.name,
                )

        # --- Clamp to the REAL exchange balance ---
        # Internal position tracking (positions.json) computes quantity from
        # the trade plan (position_size_usdt / entry_price), not from the
        # actual filled amount. Fees paid in the base asset, rounding to
        # exchange lot precision, or the position being sold already
        # (manually, or by a sibling TP/SL order) all make the tracked
        # quantity drift above the real wallet balance. Without this check
        # every TP/SL attempt fails forever with "insufficient balance"
        # and the position never gets marked closed.
        base_asset = symbol.split("/")[0]
        available = self.get_asset_balance(symbol)
        if available <= 0:
            return OrderResult.rejected(
                request,
                f"NO_BALANCE: exchange holds 0 {base_asset} — position is likely "
                f"already closed (sold manually or by another order)",
                self.name,
            )
        if amount_p > available:
            clamped = self.amount_to_precision(symbol, available)
            if clamped <= 0:
                return OrderResult.rejected(
                    request,
                    f"NO_BALANCE: available {base_asset} balance ({available}) is "
                    f"below exchange minimum after precision",
                    self.name,
                )
            amount_p = clamped

        try:
            provider = self._exchange.get_provider()
            ex = provider._get_exchange()
            price_p = None
            id_params = provider.client_order_id_params(request.client_order_id)
            ccxt_order = ex.create_order(
                symbol=symbol,
                type="market",
                side="sell",
                amount=amount_p,
                price=price_p,
                params=id_params,
            )
            elapsed = (time.time() - t0) * 1000
            status = _map_live_status(ccxt_order, amount_p)
            return OrderResult(
                order_id=str(ccxt_order.get("id", "")),
                trace_id=request.trace_id,
                execution_id=str(uuid.uuid4()),
                status=status,
                symbol=symbol,
                side="SELL",
                type=request.type,
                amount=amount_p,
                filled_amount=float(ccxt_order.get("filled", 0)),
                filled_price=float(ccxt_order.get("average") or ccxt_order.get("price", price)),
                fee=float(ccxt_order.get("fee", {}).get("cost", 0) if isinstance(ccxt_order.get("fee"), dict) else 0),
                cost=float(ccxt_order.get("cost", 0)),
                latency_ms=round(elapsed, 2),
                retries=0,
                executor=self.name,
                exchange=self.get_exchange_name(),
                mode="LIVE",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            return OrderResult.failed(request, f"Live SELL error: {exc}", self.name)


def _map_live_status(ccxt_order: dict[str, Any], requested_amount: float) -> str:
    raw_status = str(ccxt_order.get("status") or "").lower()
    filled = float(ccxt_order.get("filled", 0) or 0)
    if raw_status in ("canceled", "cancelled", "expired"):
        return "CANCELLED"
    if raw_status == "rejected":
        return "REJECTED"
    if filled <= 0:
        return "PENDING"
    if requested_amount > 0 and filled < requested_amount * 0.999:
        return "PARTIALLY_FILLED"
    return "FILLED"


# ======================================================================
#  ExecutionProvider factory
# ======================================================================


def create_execution_provider(
    mode: str,
    exchange_manager: Any = None,
    config: Any = None,
) -> ExecutionProvider:
    """Factory: returns the correct provider for the given mode."""
    if mode.upper() == "LIVE":
        if exchange_manager is None:
            raise ValueError("LiveExecutionProvider requires exchange_manager")
        return LiveExecutionProvider(exchange_manager, config)
    return PaperExecutionProvider()



