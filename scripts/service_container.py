"""Centralized Service Container — Dependency Injection for ZetBot AI.

Usage::

    container = ServiceContainer(config)
    container.bootstrap()
    container.scanner.run()
    container.pipeline.run()
"""

from __future__ import annotations

import time
from typing import Any, Optional

from scripts.exchange_manager import ExchangeManager
from scripts.exchange_providers import ExchangeAuthError
from scripts.order_manager import OrderManager
from scripts.position_status import is_open
from scripts.safety_limits import SafeGuard
from scripts.interfaces import (
    IConfigService,
    IExchangeManager,
    IHealthMonitor,
    IMetricsManager,
    INotificationManager,
    IOrderManager,
    IPositionManager,
    IRiskManager,
    IScanner,
    IStrategyManager,
    IWalletManager,
)


class ServiceContainer:
    """Holds all application services as singletons.

    Services are created once during ``bootstrap()`` and accessed via
    properties.  No service may be instantiated directly by consumers.
    """

    def __init__(self, config: Any, logger: Any = None) -> None:
        self._config = config
        self._logger = logger
        self._bootstrapped = False
        self._daemon_start_time: float = time.time()

        # SafeGuard — pre-trade safety gate
        self._safeguard: Optional[SafeGuard] = None

        # Service instances (lazy / bootstrapped)
        self._config_service: Optional[IConfigService] = None
        self._exchange: Optional[IExchangeManager] = None
        self._wallet: Optional[IWalletManager] = None
        self._scanner: Optional[IScanner] = None
        self._strategy: Optional[IStrategyManager] = None
        self._risk: Optional[IRiskManager] = None
        self._order: Optional[IOrderManager] = None
        self._position: Optional[IPositionManager] = None
        self._notification: Optional[INotificationManager] = None
        self._health: Optional[IHealthMonitor] = None
        self._metrics: Optional[IMetricsManager] = None
        self._pipeline: Any = None

    # ------------------------------------------------------------------
    #  Bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Create all services in dependency order."""
        if self._bootstrapped:
            return

        # Order matters: earlier services may be required by later ones
        self._config_service = _ConfigAdapter(self._config)
        self._exchange = ExchangeManager(
            active=self._config_service.exchange,
            api_key=self._config_service.api_key,
            api_secret=self._config_service.api_secret,
        )
        # Wallet must exist before Metrics: in LIVE mode, MetricsManager
        # needs the wallet to fetch the REAL exchange balance instead of
        # the stale paper_balance.json figure (this is what /status,
        # /wallet, /portfolio and /performance all read from).
        self._wallet = (
            _WalletAdapter(self._config_service)
            if self._config_service.paper_mode
            else _LiveWalletAdapter(self._config_service, self._exchange)
        )
        self._metrics = _MetricsAdapter(
            config=self._config_service,
            wallet=self._wallet,
            mode_provider=lambda: (
                "PAPER" if self._config_service.paper_mode else "LIVE"
            ),
        )
        self._notification = _NotificationAdapter(self._config_service)
        self._safeguard = SafeGuard(
            max_daily_loss_pct=self._config_service.max_daily_loss_pct,
            max_consecutive_losses=self._config_service.max_consecutive_losses,
            max_daily_trades=self._config_service.max_daily_trades,
            exchange_failure_window=self._config_service.exchange_failure_window_seconds,
            exchange_max_failures=self._config_service.exchange_max_failures,
            atr_spike_multiplier=self._config_service.atr_spike_multiplier,
        )
        self._safeguard.set_account_balance(self._config_service.account_balance)
        self._scanner = _ScannerAdapter(self._config_service)
        self._strategy = _StrategyAdapter()
        self._risk = _RiskAdapter(self._config_service)
        self._order = OrderManager(
            config=self._config_service,
            exchange=self._exchange,
            wallet=self._wallet,
            risk=self._risk,
            mode="PAPER" if self._config_service.paper_mode else "LIVE",
            safeguard=self._safeguard,
        )
        self._position = _PositionAdapter(self._config_service)
        self._health = None  # created by main.py, injected later
        self._scheduler = None  # created by main.py, injected later
        self._notifier = None  # injected by main.py after creation

        self._bootstrapped = True

    def inject_health(self, health: Any) -> None:
        """Inject HealthMonitor after creation (it needs the event loop)."""
        self._health = _HealthAdapter(health)

    def inject_scheduler(self, scheduler: Any) -> None:
        """Inject PipelineScheduler after creation."""
        self._scheduler = scheduler

    def inject_notifier(self, notifier: Any) -> None:
        """Inject centralized Notifier after creation."""
        self._notifier = notifier

    # ------------------------------------------------------------------
    #  Service properties
    # ------------------------------------------------------------------

    @property
    def daemon_start_time(self) -> float:
        return self._daemon_start_time

    @property
    def config(self) -> IConfigService:
        assert self._config_service is not None
        return self._config_service

    @property
    def exchange(self) -> IExchangeManager:
        assert self._exchange is not None
        return self._exchange

    @property
    def wallet(self) -> IWalletManager:
        assert self._wallet is not None
        return self._wallet

    @property
    def scanner(self) -> IScanner:
        assert self._scanner is not None
        return self._scanner

    @property
    def strategy(self) -> IStrategyManager:
        assert self._strategy is not None
        return self._strategy

    @property
    def risk(self) -> IRiskManager:
        assert self._risk is not None
        return self._risk

    @property
    def order(self) -> IOrderManager:
        assert self._order is not None
        return self._order

    @property
    def position(self) -> IPositionManager:
        assert self._position is not None
        return self._position

    @property
    def notification(self) -> INotificationManager:
        assert self._notification is not None
        return self._notification

    @property
    def health(self) -> Optional[IHealthMonitor]:
        return self._health

    @property
    def scheduler(self) -> Any:
        """Optional PipelineScheduler injected after creation."""
        return self._scheduler

    @property
    def safeguard(self) -> SafeGuard:
        assert self._safeguard is not None
        return self._safeguard

    @property
    def metrics(self) -> IMetricsManager:
        assert self._metrics is not None
        return self._metrics

    # ------------------------------------------------------------------
    #  Convenience
    # ------------------------------------------------------------------

    @property
    def pipeline(self) -> Any:
        """Lazy-create Pipeline wired with this container."""
        if self._pipeline is None:
            from scripts.pipeline import Pipeline  # noqa: PLC0415
            self._pipeline = Pipeline(self._config, self._logger, container=self)
        return self._pipeline

    def run_pipeline(self) -> list[Any]:
        pipeline = self.pipeline
        if self._notifier is not None:
            pipeline.set_notifier(self._notifier)
        return pipeline.run()

    def __repr__(self) -> str:
        return (
            f"ServiceContainer("
            f"config={'✓' if self._config_service else '✗'}, "
            f"exchange={'✓' if self._exchange else '✗'}, "
            f"wallet={'✓' if self._wallet else '✗'}, "
            f"scanner={'✓' if self._scanner else '✗'}, "
            f"strategy={'✓' if self._strategy else '✗'}, "
            f"risk={'✓' if self._risk else '✗'}, "
            f"order={'✓' if self._order else '✗'}, "
            f"position={'✓' if self._position else '✗'}, "
            f"notification={'✓' if self._notification else '✗'}, "
            f"metrics={'✓' if self._metrics else '✗'})"
        )


# ======================================================================
#  Internal adapter implementations
# ======================================================================

class _ConfigAdapter:
    """Wraps AppConfig frozen dataclass as IConfigService."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cfg, name)



class _WalletAdapter:
    """Wraps paper_trading_engine / paper_balance.json as IWalletManager."""

    def __init__(self, config: IConfigService) -> None:
        self._config = config

    def _load(self) -> dict[str, Any]:
        import json, os  # noqa: PLC0415
        path = os.path.join(self._config.data_dir, "paper_balance.json")
        try:
            with open(path) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        import json, os  # noqa: PLC0415
        path = os.path.join(self._config.data_dir, "paper_balance.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    @property
    def balance(self) -> float:
        return self._load().get("final_balance", 0.0)

    @property
    def equity(self) -> float:
        return self._load().get("final_equity", 0.0)

    @property
    def realized_pnl(self) -> float:
        return self._load().get("realized_pnl", 0.0)

    @property
    def unrealized_pnl(self) -> float:
        return self._load().get("unrealized_pnl", 0.0)

    @property
    def net_pnl(self) -> float:
        return self._load().get("net_pnl", 0.0)

    @property
    def total_return_pct(self) -> float:
        return self._load().get("total_return_pct", 0.0)

    @property
    def free_balance(self) -> float:
        return self.balance

    def reserve(self, amount: float) -> None:
        pass  # paper: no-op, tracked by engine

    def release(self, amount: float) -> None:
        pass

    def deduct(self, amount: float) -> None:
        pass

    def add(self, amount: float) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        return self._load()


_BALANCE_CACHE_TTL_SEC = 10.0  # avoid hammering the exchange on every read


class _LiveWalletAdapter:
    """Fetches the REAL account balance from the exchange via CCXT.

    Used instead of ``_WalletAdapter`` whenever ``config.paper_mode`` is
    False. Never falls back to ``paper_balance.json`` — if the live
    balance can't be fetched, it raises ``ExchangeAuthError`` rather than
    pretending the balance is zero (or stale), because either of those
    could cause the risk engine to size a position incorrectly.
    """

    def __init__(self, config: IConfigService, exchange: ExchangeManager) -> None:
        self._config = config
        self._exchange = exchange
        self._quote = (getattr(config, "quote_currency", "") or "USDT").upper()
        self._cache: dict[str, Any] = {}
        self._cache_ts = 0.0

    @staticmethod
    def _extract(raw: dict[str, Any], quote: str, field: str) -> Optional[float]:
        """Pull a numeric field out of ccxt's ``fetch_balance()`` shape.

        ccxt returns both ``{"free": {"USDT": 1.2, ...}, ...}`` and a
        flattened ``{"USDT": {"free": 1.2, "used": 0, "total": 1.2}, ...}``
        — handle whichever is present.
        """
        bucket = raw.get(field)
        if isinstance(bucket, dict) and quote in bucket:
            try:
                return float(bucket[quote])
            except (TypeError, ValueError):
                return None
        per_currency = raw.get(quote)
        if isinstance(per_currency, dict) and field in per_currency:
            try:
                return float(per_currency[field])
            except (TypeError, ValueError):
                return None
        return None

    def _snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < _BALANCE_CACHE_TTL_SEC:
            return self._cache

        provider = self._exchange.get_provider()
        # provider.fetch_balance() itself raises ExchangeAuthError when
        # credentials are set but the call fails — let that propagate.
        raw = provider.fetch_balance()
        if not raw:
            raise ExchangeAuthError(
                "Live balance fetch returned nothing — check API key, "
                "secret, and permissions before trading live."
            )

        free = self._extract(raw, self._quote, "free")
        total = self._extract(raw, self._quote, "total")
        if free is None and total is None:
            raise ExchangeAuthError(
                f"Exchange balance response has no '{self._quote}' entry — "
                f"check QUOTE_CURRENCY (currently '{self._quote}') and "
                "API permissions."
            )

        snapshot = {
            "final_balance": free if free is not None else total,
            "final_equity": total if total is not None else free,
            # Spot wallet balance alone doesn't carry trade-level PnL —
            # these stay at 0.0 until live position tracking (Phase 5+)
            # computes them from actual fills instead of paper state.
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "total_return_pct": 0.0,
        }
        self._cache = snapshot
        self._cache_ts = now
        return snapshot

    @property
    def balance(self) -> float:
        return self._snapshot()["final_balance"]

    @property
    def equity(self) -> float:
        return self._snapshot()["final_equity"]

    @property
    def realized_pnl(self) -> float:
        return self._snapshot()["realized_pnl"]

    @property
    def unrealized_pnl(self) -> float:
        return self._snapshot()["unrealized_pnl"]

    @property
    def net_pnl(self) -> float:
        return self._snapshot()["net_pnl"]

    @property
    def total_return_pct(self) -> float:
        return self._snapshot()["total_return_pct"]

    @property
    def free_balance(self) -> float:
        return self.balance

    def reserve(self, amount: float) -> None:
        pass  # the exchange itself enforces available balance at order time

    def release(self, amount: float) -> None:
        pass

    def deduct(self, amount: float) -> None:
        pass

    def add(self, amount: float) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot())


class _ScannerAdapter:
    """Wraps scripts.scanner as IScanner."""

    def __init__(self, config: IConfigService) -> None:
        self._config = config

    def run(self) -> dict[str, Any]:
        from scripts import scanner  # noqa: PLC0415
        try:
            scanner.main()
        except RuntimeError as exc:
            # "cannot schedule new futures after interpreter shutdown"
            # happens when shutdown is requested while scanner threads
            # are still running — handle gracefully
            import sys  # noqa: PLC0415
            print(f"Scanner aborted: {exc}")
            return {}
        return self.get_results()

    def get_results(self) -> dict[str, Any]:
        import json, os  # noqa: PLC0415
        path = os.path.join(self._config.data_dir, "scanner_results.json")
        try:
            with open(path) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


class _StrategyAdapter:
    """Wraps scripts.decision_engine as IStrategyManager."""

    def evaluate(self, scanner_results: dict[str, Any]) -> list[dict[str, Any]]:
        from scripts import decision_engine  # noqa: PLC0415
        engine = decision_engine.DecisionEngine()
        # The DecisionEngine reads from scanner_results.json internally
        return [d.__dict__ if hasattr(d, '__dict__') else d for d in engine.run()]

    def get_decisions(self) -> list[dict[str, Any]]:
        import json, os  # noqa: PLC0415
        path = "data/decision_results.json"
        try:
            with open(path) as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("decisions", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []


class _RiskAdapter:
    """Wraps scripts.risk_manager as IRiskManager."""

    def __init__(self, config: IConfigService) -> None:
        self._config = config

    def approve(self, decisions: list[dict[str, Any]], wallet: IWalletManager,
                positions: Any = None) -> list[dict[str, Any]]:
        from scripts import risk_manager  # noqa: PLC0415
        mgr = risk_manager.RiskManager(
            balance=wallet.balance,
            equity=wallet.equity,
            risk_per_trade=self._config.max_risk_per_trade_pct,
            max_daily_loss=5.0,
            max_positions=self._config.max_positions,
        )
        return [r.__dict__ if hasattr(r, '__dict__') else r for r in mgr.run()]

    def get_approved(self) -> list[dict[str, Any]]:
        import json, os  # noqa: PLC0415
        path = "data/risk_results.json"
        try:
            with open(path) as f:
                data = json.load(f)
                return [r for r in (data if isinstance(data, list)
                                    else data.get("results", []))
                        if r.get("approval") == "APPROVED"]
        except (FileNotFoundError, json.JSONDecodeError):
            return []



class _PositionAdapter:
    """Wraps scripts.position_manager as IPositionManager."""

    def __init__(self, config: IConfigService) -> None:
        self._config = config

    def get_open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self.get_all() if is_open(p.get("status"))]

    def get_all(self) -> list[dict[str, Any]]:
        import json, os  # noqa: PLC0415
        path = os.path.join(self._config.data_dir, "positions.json")
        try:
            with open(path) as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("positions", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def simulate(self, plan: dict[str, Any], current_price: float, **kwargs: Any) -> dict[str, Any]:
        from scripts.position_manager import PositionSimulator  # noqa: PLC0415
        # Build a TradePlan dataclass from dict
        from scripts.position_manager import TradePlan  # noqa: PLC0415
        tp = TradePlan(**{k: v for k, v in plan.items() if k in TradePlan.__dataclass_fields__})
        pos = PositionSimulator.simulate(tp, current_price, **kwargs)
        return pos.__dict__

    def update_positions(self, current_prices: dict[str, float]) -> list[dict[str, Any]]:
        positions = self.get_all()
        updated = []
        for pos in positions:
            price = current_prices.get(pos.get("symbol"))
            if price:
                pos["current_price"] = price
            updated.append(pos)
        return updated


class _NotificationAdapter:
    """Wraps Notifier as INotificationManager.

    Uses the centralized Notifier singleton from bot.notifier.
    """

    def __init__(self, config: IConfigService) -> None:
        self._config = config
        self._notifier: Any = None

    def _get_notifier(self) -> Any:
        if self._notifier is None:
            from bot.notifier import Notifier  # noqa: PLC0415
            self._notifier = Notifier.from_config(self._config)
        return self._notifier

    def send(self, message: str) -> bool:
        notifier = self._get_notifier()
        return notifier.send(message)

    def notify_buy(self, symbol: str, entry_price: float, quantity: float,
                   position_size: float, stop_loss: float, tp1: float,
                   tp2: float = 0.0, tp3: float = 0.0) -> None:
        notifier = self._get_notifier()
        notifier.notify_buy_opened(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            position_size=position_size,
            stop_loss=stop_loss,
            take_profit=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
        )

    def notify_close(self, symbol: str, pnl: float, reason: str, exit_price: float) -> None:
        notifier = self._get_notifier()
        notifier.notify_position_closed(
            symbol=symbol,
            pnl=pnl,
            exit_reason=reason,
            exit_price=exit_price,
        )

    def notify_error(self, error: str) -> None:
        notifier = self._get_notifier()
        notifier.notify_error(error)

    def notify_trade_rejected(self, symbol: str, reason: str) -> None:
        notifier = self._get_notifier()
        notifier.notify_trade_rejected(symbol, reason)

    def notify_system(self, message: str) -> None:
        notifier = self._get_notifier()
        notifier.notify_system(message)


class _MetricsAdapter:
    """Metrics collector — single source of truth for all bot statistics.

    Delegates to ``MetricsManager`` for file reads and delegates the
    legacy in-memory interface to the execution engine.
    """

    def __init__(
        self,
        config: Optional[IConfigService] = None,
        wallet: Any = None,
        mode_provider: Any = None,
    ) -> None:
        self._trades: list[dict[str, Any]] = []
        self._config = config
        from scripts.metrics_manager import MetricsManager  # noqa: PLC0415
        self._mgr = MetricsManager(
            data_dir=(config.data_dir if config else "data"),
            wallet=wallet,
            mode_provider=mode_provider,
        )

    # ------------------------------------------------------------------
    #  Account snapshot (single source of truth)
    # ------------------------------------------------------------------

    def account(self):
        return self._mgr.account()

    def balance_snapshot(self) -> dict[str, Any]:
        return self._mgr._read_balance_pb()  # noqa: SLF001

    def balance(self) -> float:
        return self._mgr.account().balance

    def equity(self) -> float:
        return self._mgr.account().equity

    def realized_pnl(self) -> float:
        return self._mgr.account().realized_pnl

    def unrealized_pnl(self) -> float:
        return self._mgr.account().unrealized_pnl

    def net_pnl(self) -> float:
        return self._mgr.account().net_pnl

    def total_return_pct(self) -> float:
        return self._mgr.account().total_return_pct

    # ------------------------------------------------------------------
    #  Positions / Trades
    # ------------------------------------------------------------------

    def open_positions_count(self) -> int:
        return self._mgr.open_positions_count()

    def closed_positions_count(self) -> int:
        return len(self._mgr.all_positions()) - self._mgr.open_positions_count()

    def open_positions(self) -> list[dict[str, Any]]:
        return self._mgr.open_positions()

    def all_positions(self) -> list[dict[str, Any]]:
        return self._mgr.all_positions()

    def closed_orders(self) -> list[dict[str, Any]]:
        return self._mgr.closed_orders()

    def best_trade(self) -> dict[str, Any]:
        cm = self._mgr.computed()
        return cm.best_trade

    def worst_trade(self) -> dict[str, Any]:
        cm = self._mgr.computed()
        return cm.worst_trade

    def today_summary(self) -> dict[str, Any]:
        return self._mgr.today_summary()

    def total_trades(self) -> int:
        return len(self._trades) or self._mgr.account().total_trades

    def winning_trades(self) -> int:
        wins = sum(1 for t in self._trades if t.get("net_pnl", 0) > 0)
        return wins or self._mgr.account().winning_trades

    def losing_trades(self) -> int:
        losses = sum(1 for t in self._trades if t.get("net_pnl", 0) < 0)
        return losses or self._mgr.account().losing_trades

    def win_rate(self) -> float:
        total = self.total_trades()
        if total > 0:
            return self.winning_trades() / total * 100.0
        return self._mgr.account().win_rate

    def profit_factor(self) -> float:
        m = self.get_metrics()
        return m.get("profit_factor", 0.0) or self._mgr.account().profit_factor

    def gross_profit(self) -> float:
        m = self.get_metrics()
        return m.get("gross_profit", 0.0) or self._mgr.account().gross_profit

    def gross_loss(self) -> float:
        m = self.get_metrics()
        return m.get("gross_loss", 0.0) or self._mgr.account().gross_loss

    # ------------------------------------------------------------------
    #  Legacy in-memory interface (used by execution engine)
    # ------------------------------------------------------------------

    def record_trade(self, order: dict[str, Any]) -> None:
        self._trades.append(order)

    def get_metrics(self) -> dict[str, Any]:
        total = len(self._trades)
        wins = sum(1 for t in self._trades if t.get("net_pnl", 0) > 0)
        losses = sum(1 for t in self._trades if t.get("net_pnl", 0) < 0)
        gross_profit = sum(t.get("net_pnl", 0) for t in self._trades if t.get("net_pnl", 0) > 0)
        gross_loss = abs(sum(t.get("net_pnl", 0) for t in self._trades if t.get("net_pnl", 0) < 0))
        return {
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": (wins / total * 100) if total > 0 else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
        }

    def reset(self) -> None:
        self._trades.clear()

    def summary(self) -> dict[str, Any]:
        a = self._mgr.account()
        return {
            "balance": a.balance,
            "equity": a.equity,
            "realized_pnl": a.realized_pnl,
            "unrealized_pnl": a.unrealized_pnl,
            "net_pnl": a.net_pnl,
            "total_return_pct": a.total_return_pct,
            "total_trades": a.total_trades,
            "winning_trades": a.winning_trades,
            "losing_trades": a.losing_trades,
            "win_rate": a.win_rate,
            "profit_factor": a.profit_factor,
            "gross_profit": a.gross_profit,
            "gross_loss": a.gross_loss,
            "open_positions": a.open_positions,
            "closed_positions": self.closed_positions_count(),
            "best_trade": self.best_trade(),
            "worst_trade": self.worst_trade(),
        }


class _HealthAdapter:
    """Wraps HealthMonitor as IHealthMonitor."""

    def __init__(self, monitor: Any) -> None:
        self._monitor = monitor
        self._start_time = getattr(monitor, '_start_time', time.time())

    @property
    def uptime_sec(self) -> int:
        return int(time.time() - self._start_time)

    def start(self) -> None:
        if hasattr(self._monitor, 'start'):
            self._monitor.start()

    def stop(self) -> None:
        if hasattr(self._monitor, 'stop'):
            self._monitor.stop()

    def snapshot(self) -> dict[str, Any]:
        if hasattr(self._monitor, 'snapshot'):
            return dict(self._monitor.snapshot())
        return {}

    def force_refresh(self) -> dict[str, Any]:
        if hasattr(self._monitor, 'force_refresh'):
            return dict(self._monitor.force_refresh())
        return {}
