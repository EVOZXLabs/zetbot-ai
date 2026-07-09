"""Execution Engine — production-grade order execution for ZetBot AI.

Architecture::

    OrderManager  ──►  ExecutionEngine  ──►  PaperExecutor
                                              SimulationExecutor
                                              LiveExecutor  (DISABLED)
                      ExchangeManager ──►  ExchangeProvider
                      RiskManager
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from scripts.exchange_manager import ExchangeManager


# ======================================================================
#  Enums & Constants
# ======================================================================


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(Enum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TradingMode(Enum):
    PAPER = "PAPER"
    SIMULATION = "SIMULATION"
    LIVE = "LIVE"


# ======================================================================
#  Models
# ======================================================================


@dataclass
class OrderRequest:
    """Universal order request model.

    Every order in the system uses this model regardless of mode.
    The ``trace_id`` links all attempts of the same logical trade.
    The ``execution_id`` distinguishes each retry.
    """

    trace_id: str = ""
    symbol: str = ""
    side: str = "BUY"
    type: str = "MARKET"
    amount: float = 0.0
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    time_in_force: str = "GTC"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())


@dataclass
class OrderResult:
    """Result of an order execution attempt."""

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


@dataclass
class AuditEntry:
    """Single entry in the execution audit trail."""

    trace_id: str
    execution_id: str
    order_id: str
    symbol: str
    side: str
    amount: float
    status: str
    executor: str
    mode: str
    exchange: str
    latency_ms: float
    retries: int
    error: Optional[str]
    timestamp: str


# ======================================================================
#  ExecutionEngine Protocol
# ======================================================================


@runtime_checkable
class IExecutionEngine(Protocol):
    """Interface every executor must satisfy."""

    @property
    def name(self) -> str: ...

    def execute(
        self,
        request: OrderRequest,
        config: Any,
        exchange: ExchangeManager,
        wallet: Any,
    ) -> OrderResult: ...


# ======================================================================
#  Base executor
# ======================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}{uid}" if prefix else uid


# ======================================================================
#  PaperExecutor
# ======================================================================


class PaperExecutor:
    """Executes orders in paper (simulated) mode.

    Uses the paper trading engine's ``ExecutionModel`` to apply fees
    and slippage, and updates the virtual wallet/positions.
    Exactly matches the current paper trading behaviour.
    """

    name = "paper"

    def execute(
        self,
        request: OrderRequest,
        config: Any,
        exchange: ExchangeManager,
        wallet: Any,
    ) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        side = request.side.upper()
        amount = request.amount

        if side not in ("BUY", "SELL"):
            return OrderResult.rejected(request, f"Invalid side: {side}", self.name)

        # Resolve current price
        price = request.price
        if price is None:
            ticker = exchange.get_ticker(symbol)
            price = ticker.get("last") or ticker.get("ask") or ticker.get("bid") or 0.0

        if price <= 0:
            return OrderResult.rejected(
                request, f"Cannot determine price for {symbol}", self.name,
            )

        try:
            # Use the paper trading engine's ExecutionModel for fee/slippage
            from scripts.paper_trading_engine import ExecutionModel  # noqa: PLC0415

            if side == "BUY":
                result = ExecutionModel.buy(price, amount)
                fill_price = result["fill_price"]
                fee = result["fee"]
                cost = result["total_cost"]
            else:
                result = ExecutionModel.sell(price, amount)
                fill_price = result["fill_price"]
                fee = result["fee"]
                cost = result["total_proceeds"]

            # Check wallet balance for BUY
            if side == "BUY" and wallet is not None:
                if cost > wallet.free_balance:
                    return OrderResult.rejected(
                        request,
                        f"Insufficient balance: need {cost:.2f}, "
                        f"have {wallet.free_balance:.2f}",
                        self.name,
                    )

            order_id = _generate_id("po_")
            elapsed = (time.time() - t0) * 1000

            return OrderResult(
                order_id=order_id,
                trace_id=request.trace_id,
                execution_id=_generate_id("exe_"),
                status="FILLED",
                symbol=symbol,
                side=side,
                type=request.type,
                amount=amount,
                filled_amount=amount,
                filled_price=round(fill_price, 8),
                fee=round(fee, 8),
                cost=round(cost, 8),
                latency_ms=round(elapsed, 2),
                retries=0,
                executor=self.name,
                exchange=exchange.name,
                mode="PAPER",
                timestamp=_now(),
            )

        except Exception as exc:
            return OrderResult.failed(
                request, f"Paper execution error: {exc}", self.name,
            )


# ======================================================================
#  SimulationExecutor
# ======================================================================


class SimulationExecutor:
    """Validates the order fully but NEVER submits.

    Checks:
      - Balance sufficiency
      - Price availability
      - Symbol validity
      - Side validity

    Returns a simulated ``OrderResult`` with status ``SIMULATED``.
    """

    name = "simulation"

    def execute(
        self,
        request: OrderRequest,
        config: Any,
        exchange: ExchangeManager,
        wallet: Any,
    ) -> OrderResult:
        t0 = time.time()
        symbol = request.symbol
        side = request.side.upper()
        amount = request.amount

        # Validate side
        if side not in ("BUY", "SELL"):
            return OrderResult.rejected(request, f"Invalid side: {side}", self.name)

        # Validate amount
        if amount <= 0:
            return OrderResult.rejected(
                request, f"Invalid amount: {amount}", self.name,
            )

        # Validate symbol / get price
        price = request.price
        if price is None:
            ticker = exchange.get_ticker(symbol)
            price = ticker.get("last") or ticker.get("ask") or ticker.get("bid") or 0.0

        if price <= 0:
            return OrderResult.rejected(
                request, f"Cannot determine price for {symbol}", self.name,
            )

        # Validate balance
        if wallet is not None:
            required = amount * price
            if side == "BUY" and required > wallet.free_balance:
                return OrderResult.rejected(
                    request,
                    f"Simulation: insufficient balance: need {required:.2f}, "
                    f"have {wallet.free_balance:.2f}",
                    self.name,
                )

        elapsed = (time.time() - t0) * 1000
        return OrderResult(
            order_id=_generate_id("sim_"),
            trace_id=request.trace_id,
            execution_id=_generate_id("exe_"),
            status="SIMULATED",
            symbol=symbol,
            side=side,
            type=request.type,
            amount=amount,
            filled_amount=0.0,
            filled_price=0.0,
            fee=0.0,
            cost=0.0,
            latency_ms=round(elapsed, 2),
            retries=0,
            executor=self.name,
            exchange=exchange.name,
            mode="SIMULATION",
            timestamp=_now(),
        )


# ======================================================================
#  LiveExecutor
# ======================================================================


class LiveExecutor:
    """Live exchange order execution.

    DISABLED by default.  Set ``ENABLE_LIVE_TRADING=true`` and provide
    valid API credentials to activate.

    When disabled, every ``execute()`` call returns a REJECTED result.
    When enabled, validates credentials and submits via CCXT.
    """

    name = "live"
    ENABLED = False

    @classmethod
    def is_enabled(cls) -> bool:
        return cls.ENABLED

    @classmethod
    def enable(cls) -> None:
        cls.ENABLED = True

    @classmethod
    def disable(cls) -> None:
        cls.ENABLED = False

    def execute(
        self,
        request: OrderRequest,
        config: Any,
        exchange: ExchangeManager,
        wallet: Any,
    ) -> OrderResult:
        if not self.ENABLED:
            return OrderResult.rejected(
                request,
                "Live trading is not enabled. "
                "Set ENABLE_LIVE_TRADING=true and provide valid API credentials.",
                self.name,
            )

        t0 = time.time()
        symbol = request.symbol
        side = request.side.upper()
        amount = request.amount

        if side not in ("BUY", "SELL"):
            return OrderResult.rejected(request, f"Invalid side: {side}", self.name)

        if amount <= 0:
            return OrderResult.rejected(
                request, f"Invalid amount: {amount}", self.name,
            )

        # Validate API credentials on the active provider
        try:
            provider = exchange.get_provider()
            balance = provider.fetch_balance()
            if not balance or balance.get("free") is None:
                return OrderResult.rejected(
                    request,
                    "Cannot validate live balance — check API credentials.",
                    self.name,
                )
        except Exception as exc:
            return OrderResult.failed(
                request, f"Live API check failed: {exc}", self.name,
            )

        price = request.price
        if price is None:
            ticker = exchange.get_ticker(symbol)
            price = ticker.get("last") or ticker.get("ask") or 0.0

        if price <= 0:
            return OrderResult.rejected(
                request, f"Cannot determine price for {symbol}", self.name,
            )

        try:
            ex = provider._get_exchange()
            order_type = "market" if request.type == "MARKET" else "limit"
            ccxt_order = ex.create_order(
                symbol=symbol,
                type=order_type,
                side=side.lower(),
                amount=amount,
                price=price if request.type == "LIMIT" else None,
            )

            elapsed = (time.time() - t0) * 1000
            return OrderResult(
                order_id=str(ccxt_order.get("id", _generate_id("live_"))),
                trace_id=request.trace_id,
                execution_id=_generate_id("exe_"),
                status="EXECUTED",
                symbol=symbol,
                side=side,
                type=request.type,
                amount=amount,
                filled_amount=float(ccxt_order.get("filled", 0)),
                filled_price=float(ccxt_order.get("price", price)),
                fee=float(ccxt_order.get("fee", {}).get("cost", 0)),
                cost=float(ccxt_order.get("cost", 0)),
                latency_ms=round(elapsed, 2),
                retries=0,
                executor=self.name,
                exchange=exchange.name,
                mode="LIVE",
                timestamp=_now(),
            )

        except Exception as exc:
            return OrderResult.failed(
                request, f"Live execution error: {exc}", self.name,
            )


# ======================================================================
#  ExecutionEngine — selects executor and orchestrates execution
# ======================================================================


class ExecutionEngine:
    """Orchestrates order execution by selecting the correct executor.

    Decision logic::

        mode == "LIVE"       →  LiveExecutor  (if enabled)
        mode == "SIMULATION" →  SimulationExecutor
        mode == "PAPER"      →  PaperExecutor
    """

    def __init__(
        self,
        exchange: ExchangeManager,
        config: Any,
        wallet: Any,
        mode: str = "PAPER",
    ) -> None:
        self._exchange = exchange
        self._config = config
        self._wallet = wallet
        self._mode = mode.upper()

        self._executors: dict[str, IExecutionEngine] = {
            "paper": PaperExecutor(),
            "simulation": SimulationExecutor(),
            "live": LiveExecutor(),
        }

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        upper = mode.upper()
        if upper not in ("PAPER", "SIMULATION", "LIVE"):
            raise ValueError(f"Invalid trading mode: {mode}")
        self._mode = upper

    def execute(
        self,
        request: OrderRequest,
    ) -> OrderResult:
        executor = self._select_executor()
        return executor.execute(request, self._config, self._exchange, self._wallet)

    def _select_executor(self) -> IExecutionEngine:
        mode = self._mode

        if mode == "LIVE":
            if LiveExecutor.is_enabled():
                return self._executors["live"]
            return self._executors["simulation"]

        if mode == "SIMULATION":
            return self._executors["simulation"]

        return self._executors["paper"]

    def validate_live_ready(self) -> Optional[str]:
        """Return an error message if LIVE mode is misconfigured, else None."""
        if self._mode != "LIVE":
            return None
        if not LiveExecutor.is_enabled():
            return (
                "Live trading is not enabled. "
                "Set ENABLE_LIVE_TRADING=true to activate."
            )
        # Check API credentials by attempting a balance fetch
        try:
            provider = self._exchange.get_provider()
            provider.fetch_balance()
        except Exception as exc:
            return f"Live API check failed: {exc}"
        # Check that API keys are configured
        api_key = getattr(self._config, "api_key", "") or ""
        api_secret = getattr(self._config, "api_secret", "") or ""
        if not api_key or not api_secret:
            return (
                "Live trading requires API_KEY and API_SECRET environment variables."
            )
        return None

    def enable_live(self) -> None:
        LiveExecutor.enable()

    def disable_live(self) -> None:
        LiveExecutor.disable()

    def is_live_enabled(self) -> bool:
        return LiveExecutor.is_enabled()


# ======================================================================
#  Audit trail
# ======================================================================


AUDIT_PATH = "data/execution_audit.jsonl"


def append_audit(entry: AuditEntry) -> None:
    """Append a single audit entry to the JSONL audit file."""
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")


def read_audit(limit: int = 100) -> list[AuditEntry]:
    """Read the most recent *limit* audit entries."""
    if not os.path.exists(AUDIT_PATH):
        return []
    entries: list[AuditEntry] = []
    with open(AUDIT_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    data = json.loads(line)
                    entries.append(AuditEntry(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
    return entries[-limit:]


# ======================================================================
#  Execution metrics
# ======================================================================


class ExecutionMetrics:
    """In-memory execution metrics collector."""

    def __init__(self) -> None:
        self._executions: list[OrderResult] = []

    def record(self, result: OrderResult) -> None:
        self._executions.append(result)

    def summary(self) -> dict[str, Any]:
        total = len(self._executions)
        if total == 0:
            return {
                "total": 0,
                "filled": 0,
                "rejected": 0,
                "failed": 0,
                "simulated": 0,
                "avg_latency_ms": 0.0,
                "total_retries": 0,
                "by_executor": {},
            }
        filled = sum(1 for e in self._executions if e.status == "FILLED")
        rejected = sum(1 for e in self._executions if e.status == "REJECTED")
        failed = sum(1 for e in self._executions if e.status == "FAILED")
        simulated = sum(1 for e in self._executions if e.status == "SIMULATED")
        avg_latency = sum(e.latency_ms for e in self._executions) / total
        total_retries = sum(e.retries for e in self._executions)

        by_executor: dict[str, int] = {}
        for e in self._executions:
            by_executor[e.executor] = by_executor.get(e.executor, 0) + 1

        return {
            "total": total,
            "filled": filled,
            "rejected": rejected,
            "failed": failed,
            "simulated": simulated,
            "avg_latency_ms": round(avg_latency, 2),
            "total_retries": total_retries,
            "by_executor": by_executor,
            "exchange": self._executions[-1].exchange if self._executions else "",
        }

    def reset(self) -> None:
        self._executions.clear()
