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
from scripts.order_manager import OrderManager
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
        )
        self._metrics = _MetricsAdapter(config=self._config_service)
        self._notification = _NotificationAdapter(self._config_service)
        self._wallet = _WalletAdapter(self._config_service)
        self._scanner = _ScannerAdapter(self._config_service)
        self._strategy = _StrategyAdapter()
        self._risk = _RiskAdapter(self._config_service)
        self._order = OrderManager(
            config=self._config_service,
            exchange=self._exchange,
            wallet=self._wallet,
            risk=self._risk,
            mode="PAPER",
        )
        self._position = _PositionAdapter(self._config_service)
        self._health = None  # created by main.py, injected later
        self._scheduler = None  # created by main.py, injected later

        self._bootstrapped = True

    def inject_health(self, health: Any) -> None:
        """Inject HealthMonitor after creation (it needs the event loop)."""
        self._health = _HealthAdapter(health)

    def inject_scheduler(self, scheduler: Any) -> None:
        """Inject PipelineScheduler after creation."""
        self._scheduler = scheduler

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
        return self.pipeline.run()

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
        return [p for p in self.get_all() if p.get("status") == "OPEN"]

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
    """Wraps TelegramNotifier as INotificationManager."""

    def __init__(self, config: IConfigService) -> None:
        self._config = config

    def _get_notifier(self) -> Any:
        from bot.telegram import TelegramNotifier  # noqa: PLC0415
        return TelegramNotifier()

    def send(self, message: str) -> bool:
        notifier = self._get_notifier()
        return notifier.send(message) if hasattr(notifier, 'send') else False

    def notify_buy(self, symbol: str, entry_price: float, quantity: float,
                   position_size: float, stop_loss: float, tp1: float) -> None:
        notifier = self._get_notifier()
        if hasattr(notifier, 'notify_buy'):
            notifier.notify_buy(symbol, entry_price, quantity,
                                position_size, stop_loss, tp1)
        else:
            self.send(
                f"\U0001f4b0 *BUY OPENED*\n"
                f"Symbol: `{symbol}`\n"
                f"Entry: `{entry_price:.6f}`\n"
                f"Size: `{position_size:.2f} USDT`\n"
                f"SL: `{stop_loss:.6f}`\n"
                f"TP: `{tp1:.6f}`"
            )

    def notify_close(self, symbol: str, pnl: float, reason: str, exit_price: float) -> None:
        notifier = self._get_notifier()
        if hasattr(notifier, 'notify_close'):
            notifier.notify_close(symbol, pnl, reason, exit_price)
        else:
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            self.send(
                f"{emoji} *POSITION CLOSED*\n"
                f"Symbol: `{symbol}`\n"
                f"PnL: `${pnl:+,.2f}`\n"
                f"Reason: `{reason}`\n"
                f"Exit: `{exit_price:.6f}`"
            )

    def notify_error(self, error: str) -> None:
        notifier = self._get_notifier()
        if hasattr(notifier, 'notify_error'):
            notifier.notify_error(error)
        else:
            self.send(f"\u26a0\ufe0f *Error*\n`{error}`")


class _MetricsAdapter:
    """Metrics collector — single source of truth for all bot statistics.

    Reads from the canonical JSON files written by the pipeline and engine so
    that every command sees the same values.
    """

    def __init__(self, config: Optional[IConfigService] = None) -> None:
        self._trades: list[dict[str, Any]] = []
        self._config = config

    # ------------------------------------------------------------------
    #  JSON file readers (unified source of truth)
    # ------------------------------------------------------------------

    def _data_dir(self) -> str:
        if self._config is not None:
            return self._config.data_dir
        return "data"

    def _read_json(self, filename: str) -> dict[str, Any]:
        import json, os  # noqa: PLC0415
        path = os.path.join(self._data_dir(), filename)
        try:
            with open(path) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _read_orders(self) -> list[dict[str, Any]]:
        d = self._read_json("paper_orders.json")
        return d if isinstance(d, list) else d.get("orders", [])

    def _read_positions(self) -> list[dict[str, Any]]:
        d = self._read_json("positions.json")
        return d if isinstance(d, list) else d.get("positions", [])

    # ------------------------------------------------------------------
    #  Balance / PnL (from paper_balance.json)
    # ------------------------------------------------------------------

    def balance_snapshot(self) -> dict[str, Any]:
        return self._read_json("paper_balance.json")

    @property
    def balance(self) -> float:
        return self.balance_snapshot().get("final_balance", 0.0)

    @property
    def equity(self) -> float:
        pb = self.balance_snapshot()
        bal = pb.get("final_balance", 0.0)
        if self.open_positions_count() == 0:
            return bal
        return bal + pb.get("unrealized_pnl", 0.0)

    @property
    def realized_pnl(self) -> float:
        return self.balance_snapshot().get("realized_pnl", 0.0)

    @property
    def unrealized_pnl(self) -> float:
        pb = self.balance_snapshot()
        if self.open_positions_count() == 0:
            return 0.0
        return pb.get("unrealized_pnl", 0.0)

    @property
    def net_pnl(self) -> float:
        pb = self.balance_snapshot()
        realized = pb.get("realized_pnl", 0.0)
        if self.open_positions_count() == 0:
            return realized
        return realized + pb.get("unrealized_pnl", 0.0)

    @property
    def total_return_pct(self) -> float:
        return self.balance_snapshot().get("total_return_pct", 0.0)

    # ------------------------------------------------------------------
    #  Legacy in-memory methods (used by tests & execution engine)
    # ------------------------------------------------------------------

    def total_trades(self) -> int:
        return len(self._trades) or self.balance_snapshot().get("total_trades", 0)

    def winning_trades(self) -> int:
        wins = sum(1 for t in self._trades if t.get("net_pnl", 0) > 0)
        return wins or self.balance_snapshot().get("winning_trades", 0)

    def losing_trades(self) -> int:
        losses = sum(1 for t in self._trades if t.get("net_pnl", 0) < 0)
        return losses or self.balance_snapshot().get("losing_trades", 0)

    def win_rate(self) -> float:
        total = self.total_trades()
        if total > 0:
            return self.winning_trades() / total * 100.0
        return self.balance_snapshot().get("win_rate", 0.0)

    def profit_factor(self) -> float:
        m = self.get_metrics()
        pf = m.get("profit_factor", 0.0)
        return pf or self.balance_snapshot().get("profit_factor", 0.0)

    def gross_profit(self) -> float:
        m = self.get_metrics()
        gp = m.get("gross_profit", 0.0)
        return gp or self.balance_snapshot().get("gross_profit", 0.0)

    def gross_loss(self) -> float:
        m = self.get_metrics()
        gl = m.get("gross_loss", 0.0)
        return gl or self.balance_snapshot().get("gross_loss", 0.0)

    # ------------------------------------------------------------------
    #  File-based accessors (single source of truth for commands)
    # ------------------------------------------------------------------

    def open_positions_count(self) -> int:
        return sum(1 for p in self._read_positions() if p.get("status") == "OPEN")

    def closed_positions_count(self) -> int:
        return sum(
            1 for p in self._read_positions()
            if p.get("status") in ("CLOSED", "STOPPED", "TIMEOUT")
        )

    def balance(self) -> float:
        return self.balance_snapshot().get("final_balance", 0.0)

    def equity(self) -> float:
        pb = self.balance_snapshot()
        bal = pb.get("final_balance", 0.0)
        if self.open_positions_count() == 0:
            return bal
        return bal + pb.get("unrealized_pnl", 0.0)

    def realized_pnl(self) -> float:
        return self.balance_snapshot().get("realized_pnl", 0.0)

    def unrealized_pnl(self) -> float:
        pb = self.balance_snapshot()
        if self.open_positions_count() == 0:
            return 0.0
        return pb.get("unrealized_pnl", 0.0)

    def net_pnl(self) -> float:
        pb = self.balance_snapshot()
        realized = pb.get("realized_pnl", 0.0)
        if self.open_positions_count() == 0:
            return realized
        return realized + pb.get("unrealized_pnl", 0.0)

    def total_return_pct(self) -> float:
        return self.balance_snapshot().get("total_return_pct", 0.0)

    def open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self._read_positions() if p.get("status") == "OPEN"]

    def all_positions(self) -> list[dict[str, Any]]:
        return self._read_positions()

    def closed_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._read_orders() if o.get("status") == "CLOSED"]

    def best_trade(self) -> dict[str, Any]:
        closed = self.closed_orders()
        if not closed:
            return {}
        return max(closed, key=lambda o: o.get("net_pnl", 0))

    def worst_trade(self) -> dict[str, Any]:
        closed = self.closed_orders()
        if not closed:
            return {}
        return min(closed, key=lambda o: o.get("net_pnl", 0))

    # ------------------------------------------------------------------
    #  Legacy interface (in-memory trades, still used by execution engine)
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

    # ------------------------------------------------------------------
    #  Full unified summary
    # ------------------------------------------------------------------

    def _derive_equity(self, balance: float, unrealized_pnl: float) -> float:
        """If no open positions, equity = balance and unrealized pnl = 0."""
        if self.open_positions_count() == 0:
            return balance
        return balance + unrealized_pnl

    def summary(self) -> dict[str, Any]:
        pb = self.balance_snapshot()
        bal = pb.get("final_balance", 0.0)
        raw_unrealized = pb.get("unrealized_pnl", 0.0)
        realized = pb.get("realized_pnl", 0.0)
        net_pnl = pb.get("net_pnl", 0.0)
        # Adjust: if no open positions, unrealized = 0 and equity = balance
        open_count = self.open_positions_count()
        if open_count == 0:
            unrealized = 0.0
        else:
            unrealized = raw_unrealized
        equity = bal + unrealized
        return {
            "balance": bal,
            "equity": equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "net_pnl": realized + unrealized if open_count > 0 else realized,
            "total_return_pct": pb.get("total_return_pct", 0.0),
            "total_trades": pb.get("total_trades", 0),
            "winning_trades": pb.get("winning_trades", 0),
            "losing_trades": pb.get("losing_trades", 0),
            "win_rate": pb.get("win_rate", 0.0),
            "profit_factor": pb.get("profit_factor", 0.0),
            "gross_profit": pb.get("gross_profit", 0.0),
            "gross_loss": pb.get("gross_loss", 0.0),
            "open_positions": open_count,
            "closed_positions": self.closed_positions_count,
            "closed_positions": self.closed_positions_count,
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
