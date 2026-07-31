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
    The ``client_order_id`` is sent to the exchange on every LIVE attempt
    for this request so a retry can never result in a duplicate fill —
    see ``OrderManager._find_existing_live_order``.
    """

    trace_id: str = ""
    client_order_id: str = ""
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
        if not self.client_order_id:
            # Deterministic from trace_id so every retry of the SAME
            # logical order reuses the same exchange-side client id.
            # Prefixed + truncated to stay within Binance's 36-char limit.
            self.client_order_id = "zb" + self.trace_id.replace("-", "")[:34]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _map_live_order_status(ccxt_order: dict[str, Any], requested_amount: float) -> str:
    """Map a raw ccxt ``create_order`` response to our ``OrderStatus``.

    A market order is NOT guaranteed to be fully filled by the time
    ``create_order`` returns — some exchanges (or ccxt's own response)
    can report ``open`` or a partial ``filled`` amount. This must not be
    reported as a blanket "EXECUTED"/"FILLED", or downstream position
    tracking will assume the trade fully settled when it may not have.

    This is a best-effort snapshot of the create_order response only —
    it is NOT a substitute for polling fetch_order() later to confirm
    final settlement (order reconciliation — not implemented yet).
    """
    raw_status = str(ccxt_order.get("status") or "").lower()
    filled = float(ccxt_order.get("filled", 0) or 0)

    if raw_status in ("canceled", "cancelled", "expired"):
        return "CANCELLED"
    if raw_status == "rejected":
        return "REJECTED"

    if filled <= 0:
        return "PENDING"
    # Small tolerance for float / exchange rounding, not a real gap.
    if requested_amount > 0 and filled < requested_amount * 0.999:
        return "PARTIALLY_FILLED"
    return "FILLED"


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

    DISABLED by default. Armed only via the operator confirmation flow:
    ``/golive`` then replying ``CONFIRM LIVE`` (see
    ``telegram/commands/live.py`` and ``OrderManager.arm_live()``).

    When disabled, every ``execute()`` call returns a REJECTED result.
    When enabled, validates credentials/balance and submits via CCXT.
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
                "Live trading is not enabled (not armed). "
                "Run /golive and reply CONFIRM LIVE to arm real-money trading.",
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

        provider = exchange.get_provider()

        price = request.price
        if price is None:
            ticker = exchange.get_ticker(symbol)
            price = ticker.get("last") or ticker.get("ask") or 0.0

        if price <= 0:
            return OrderResult.rejected(
                request, f"Cannot determine price for {symbol}", self.name,
            )

        # Validate the account is reachable/authenticated and (when
        # possible) has enough balance — BEFORE submitting the order.
        #
        # Prefer the wallet adapter (LiveWalletAdapter has a ~10s cache)
        # over calling provider.fetch_balance() directly on every single
        # order: that was one extra authenticated API round-trip per
        # order, adding latency and eating into exchange rate limits for
        # no benefit once the wallet's own cache already exists.
        free_balance: Optional[float] = None
        try:
            if wallet is not None:
                free_balance = wallet.free_balance
            else:
                # No wallet adapter wired in (e.g. direct/test usage) —
                # fall back to a raw check so we never skip validation.
                balance = provider.fetch_balance()
                if not balance or balance.get("free") is None:
                    return OrderResult.rejected(
                        request,
                        "Cannot validate live balance — check API credentials.",
                        self.name,
                    )
        except Exception as exc:
            return OrderResult.failed(
                request, f"Live balance check failed: {exc}", self.name,
            )

        if side == "BUY" and free_balance is not None:
            est_cost = amount * price
            if est_cost > free_balance:
                return OrderResult.rejected(
                    request,
                    f"Insufficient balance: need ~{est_cost:.2f}, "
                    f"have {free_balance:.2f}",
                    self.name,
                )

        try:
            ex = provider._get_exchange()
            order_type = "market" if request.type == "MARKET" else "limit"

            # Round to the exchange's lot-size / tick-size BEFORE sending,
            # or Binance/others reject with LOT_SIZE / PRICE_FILTER errors.
            precise_amount = provider.amount_to_precision(symbol, amount)
            precise_price = (
                provider.price_to_precision(symbol, price)
                if request.type == "LIMIT" else None
            )

            # Tag the order with our client_order_id so a retry can check
            # "did this already land?" instead of blindly resubmitting —
            # see OrderManager._find_existing_live_order.
            id_params = provider.client_order_id_params(request.client_order_id)

            ccxt_order = ex.create_order(
                symbol=symbol,
                type=order_type,
                side=side.lower(),
                amount=precise_amount,
                price=precise_price,
                params=id_params,
            )

            elapsed = (time.time() - t0) * 1000
            # NOTE: this status reflects ccxt's response to create_order()
            # ONLY. Market orders are not always instantly final on every
            # exchange — the response can still say "open"/partially
            # filled. This is a best-effort snapshot, not a settlement
            # confirmation; a later reconciliation pass (polling
            # fetch_order by id/client_order_id) is needed before treating
            # a position as fully opened/closed. Not implemented yet.
            status = _map_live_order_status(ccxt_order, precise_amount)
            return OrderResult(
                order_id=str(ccxt_order.get("id", _generate_id("live_"))),
                trace_id=request.trace_id,
                execution_id=_generate_id("exe_"),
                status=status,
                symbol=symbol,
                side=side,
                type=request.type,
                amount=precise_amount,
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


class OnchainExecutor:
    """LIVE execution for on-chain (DEX) trades.

    Unlike ``LiveExecutor`` (CCXT order lifecycle: submit → poll →
    reconcile — see its docstring), a DEX swap is a single atomic
    transaction: it either lands in one block or it doesn't. There is
    no order id to poll, no partial fill. This executor therefore
    returns a settled ``OrderResult`` immediately rather than a
    "PENDING" status implying a reconciliation pass that doesn't apply
    here.

    Single source of truth for the live-trading gate: this executor
    keeps no separate ENABLED flag of its own. It defers entirely to
    ``config.onchain_live_confirmed`` (``ONCHAIN_LIVE_CONFIRMED`` in
    ``.env``) — the exact same flag ``onchain_providers.swap()``
    itself checks — so there is exactly one switch that arms on-chain
    live trading, never two that could drift out of sync.

    Routing (which chain / contract address a symbol maps to) comes
    from ``data/onchain_symbol_map.json``, written by the scanner
    whenever on-chain pairs are scanned — not duplicated here.
    """

    name = "onchain"
    ROUTE_MAP_PATH = "data/onchain_symbol_map.json"

    @staticmethod
    def is_enabled(config: Any) -> bool:
        return bool(getattr(config, "onchain_live_confirmed", False))

    def _load_route(self, symbol: str) -> Optional[dict[str, str]]:
        try:
            with open(self.ROUTE_MAP_PATH) as f:
                mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return mapping.get(symbol)

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

        if side not in ("BUY", "SELL"):
            return OrderResult.rejected(request, f"Invalid side: {side}", self.name)
        if request.amount <= 0:
            return OrderResult.rejected(
                request, f"Invalid amount: {request.amount}", self.name,
            )
        if not self.is_enabled(config):
            return OrderResult.rejected(
                request,
                "On-chain live trading is not confirmed. Set "
                "ONCHAIN_LIVE_CONFIRMED=true in .env only after testing "
                "on a testnet with a throwaway wallet.",
                self.name,
            )

        route = self._load_route(symbol)
        if not route:
            return OrderResult.rejected(
                request,
                f"No on-chain route for {symbol} — run the scanner with "
                "on-chain enabled first (it writes "
                f"{self.ROUTE_MAP_PATH}).",
                self.name,
            )

        chain = route.get("chain", "")
        token_address = route.get("token_address", "")
        quote_address = route.get("quote_address", "")
        if not (chain and token_address and quote_address):
            return OrderResult.rejected(
                request, f"Incomplete on-chain route for {symbol}: {route}", self.name,
            )

        price = request.price or 0.0
        if price <= 0:
            return OrderResult.rejected(
                request, f"Cannot determine price for {symbol}", self.name,
            )

        try:
            from scripts.onchain_providers import (  # noqa: PLC0415
                LiveNotConfirmedError, OnchainAuthError, get_onchain_provider,
            )
            provider = get_onchain_provider(chain, config)
        except Exception as exc:
            return OrderResult.failed(
                request, f"Could not build on-chain provider for {chain}: {exc}", self.name,
            )

        try:
            # BUY spends an exact quote-token notional to acquire
            # whatever token amount results (matches how position
            # sizing here works — a USD amount, not an exact token
            # quantity). SELL disposes of an exact token quantity
            # (the position being closed) for whatever quote it fetches.
            if side == "BUY":
                notional = request.amount * price
                tx = provider.swap_exact_in(config, quote_address, token_address, notional)
            else:
                tx = provider.swap_exact_in(config, token_address, quote_address, request.amount)
        except LiveNotConfirmedError as exc:
            return OrderResult.rejected(request, str(exc), self.name)
        except OnchainAuthError as exc:
            return OrderResult.failed(request, str(exc), self.name)
        except Exception as exc:
            return OrderResult.failed(request, f"On-chain swap failed: {exc}", self.name)

        elapsed = (time.time() - t0) * 1000
        tx_ref = tx.get("tx_hash") or tx.get("tx_signature") or ""
        settled = tx.get("status") in ("success", "submitted")

        return OrderResult(
            order_id=tx_ref or _generate_id("onchain_"),
            trace_id=request.trace_id,
            execution_id=_generate_id("exe_"),
            status="FILLED" if settled else "FAILED",
            symbol=symbol,
            side=side,
            type=request.type,
            amount=request.amount,
            filled_amount=request.amount if settled else 0.0,
            filled_price=price,
            # Gas is paid directly from the wallet's native balance, not
            # deducted as a %-fee the way a CEX taker fee is — tracked
            # on-chain via the tx itself, not modeled here.
            fee=0.0,
            cost=request.amount * price,
            error=None if settled else f"swap status: {tx.get('status')}",
            latency_ms=round(elapsed, 2),
            retries=0,
            executor=self.name,
            exchange=f"onchain:{chain}",
            mode="LIVE",
            timestamp=_now(),
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
            "onchain": OnchainExecutor(),
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
        executor = self._select_executor(request)
        return executor.execute(request, self._config, self._exchange, self._wallet)

    def _select_executor(self, request: OrderRequest) -> IExecutionEngine:
        mode = self._mode
        is_onchain = request.metadata.get("venue") == "onchain"

        if mode == "LIVE":
            if is_onchain:
                # Always routes here regardless of OnchainExecutor's own
                # live-confirmation state — it returns a clean REJECTED
                # OrderResult itself if ONCHAIN_LIVE_CONFIRMED isn't set,
                # the same pattern LiveExecutor uses for CEX trades.
                return self._executors["onchain"]
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
                "Live trading is not enabled (not armed). "
                "Run /golive and reply CONFIRM LIVE to activate."
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

    def live_readiness_report(self) -> dict[str, Any]:
        """Full read-only diagnostic for /livecheck and /golive.

        This NEVER arms anything — it only checks whether arming would
        succeed. Every early-exit path leaves ``ready: False`` in place.
        """
        quote = (getattr(self._config, "quote_currency", "") or "USDT").upper()
        report: dict[str, Any] = {
            "mode": self._mode,
            "armed": LiveExecutor.is_enabled(),
            "exchange": getattr(self._exchange, "name", ""),
            "api_key_set": bool(
                (getattr(self._config, "api_key", "") or "")
                and (getattr(self._config, "api_secret", "") or ""),
            ),
            "currency": quote,
            "balance": None,
            "connected": False,
            "trading_permission": None,  # True / False / None(unknown)
            "ready": False,
            "reasons": [],
        }

        if self._mode != "LIVE":
            report["reasons"].append(
                f"Engine mode is {self._mode}, not LIVE — set "
                "PAPER_MODE=false and restart the bot first.",
            )
            return report

        if not report["api_key_set"]:
            report["reasons"].append("API_KEY / API_SECRET not configured.")
            return report

        try:
            provider = self._exchange.get_provider()
            raw = provider.fetch_balance()
        except Exception as exc:
            report["reasons"].append(f"Exchange connection/auth failed: {exc}")
            return report

        if not raw:
            report["reasons"].append(
                "Balance fetch returned nothing — check API permissions.",
            )
            return report

        report["connected"] = True

        free = None
        bucket = raw.get("free")
        if isinstance(bucket, dict) and quote in bucket:
            free = bucket[quote]
        else:
            per_currency = raw.get(quote)
            if isinstance(per_currency, dict):
                free = per_currency.get("free")
        report["balance"] = free
        if free is None:
            report["reasons"].append(
                f"Could not find '{quote}' in the balance response — "
                "check QUOTE_CURRENCY.",
            )

        # Best-effort trading-permission check. Not every exchange exposes
        # this uniformly through ccxt, so "unknown" is a valid, honest
        # outcome — never assumed to be OK.
        can_trade: Optional[bool] = None
        info = raw.get("info")
        if isinstance(info, dict):
            if "canTrade" in info:
                can_trade = bool(info.get("canTrade"))
            elif "permissions" in info and isinstance(info.get("permissions"), list):
                can_trade = "SPOT" in info["permissions"]
        report["trading_permission"] = can_trade
        if can_trade is False:
            report["reasons"].append(
                "Exchange account reports trading is NOT permitted "
                "(read-only / restricted API key?).",
            )

        # Withdrawal permission must NEVER be present on a bot's API key —
        # if the key (and thus this running process / its on-disk secrets)
        # is ever compromised, a withdrawal-capable key lets funds be
        # moved off-exchange entirely, not just traded. Treat this as
        # disqualifying rather than a soft warning.
        #
        # IMPORTANT: do NOT use info["canWithdraw"] here. That field
        # (from the account endpoint underlying fetch_balance()) reflects
        # the ACCOUNT's overall withdrawal capability, not what THIS
        # specific API key is actually permitted to do — it is commonly
        # `true` even for a read-only key. See:
        # https://dev.binance.vision/t/how-to-validate-an-api-key-permissions/1519
        # The only reliable source is the dedicated per-key permissions
        # endpoint (fetch_api_key_permissions() -> enableWithdrawals).
        can_withdraw: Optional[bool] = None
        try:
            key_perms = provider.fetch_api_key_permissions()
        except Exception:
            key_perms = {}
        if "enableWithdrawals" in key_perms:
            can_withdraw = bool(key_perms["enableWithdrawals"])
        elif isinstance(info, dict) and isinstance(info.get("permissions"), list):
            # Fallback for exchanges without a dedicated permissions
            # endpoint: Binance's unified "permissions" list (e.g.
            # ["SPOT"]) does sometimes include "WITHDRAWALS" explicitly.
            can_withdraw = "WITHDRAWALS" in info["permissions"]
        report["can_withdraw"] = can_withdraw
        if can_withdraw is True:
            report["reasons"].append(
                "API key has WITHDRAWAL permission — this is unsafe for "
                "an automated bot. Create a new key with trading-only "
                "permission (no withdrawal) before going live.",
            )

        report["ready"] = (
            not report["reasons"] and report["connected"] and free is not None
        )
        return report


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
