"""
Pipeline orchestrator for ZetBot AI.

Sequentially executes all six analysis stages:

    scanner -> decision_engine -> risk_manager -> trade_executor -> position_manager -> paper_trading_engine

Each stage runs the existing module's ``main()`` function.  If any stage
fails the pipeline stops immediately with a clear error.  Module-level
configuration constants are overridden from the central ``AppConfig``
before each stage.

Usage::

    from scripts.pipeline import Pipeline
    pipeline = Pipeline(config, logger)
    results = pipeline.run()
"""

import concurrent.futures
import importlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from scripts.app_config import AppConfig
from scripts.decision_trace import DecisionTrace, DecisionTraceEntry
from scripts.logger import PipelineLogger


STAGE_TIMEOUT = 300  # maximum seconds per pipeline stage
SCANNER_OUTPUT = "data/scanner_results.json"


def _touch_file(path: str) -> None:
    """Update file mtime to now (create if missing)."""
    try:
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


@dataclass
class StageResult:
    """Result of a single pipeline stage execution."""
    name: str
    success: bool
    duration: float
    detail: str = ""
    error: Optional[str] = None


# Map of module name -> { attribute_name: config_value }
_CONFIG_OVERRIDES: dict[str, dict[str, str]] = {
    "scripts.scanner": {
        "THREADS": "scanner_threads",
        "TOP_N": "scanner_top_n",
        "MIN_VOLUME_24H": "scanner_min_volume",
    },
    "scripts.decision_engine": {
        "TOP_N": "decision_top_n",
    },
    "scripts.risk_manager": {
        "ACCOUNT_BALANCE": "account_balance",
        "MAX_OPEN_POSITIONS": "max_positions",
        "MIN_RR": "min_rr",
        "MAX_RR": "max_rr",
        "MIN_PROBABILITY": "min_probability",
        "MAX_ATR_PCT": "max_atr_pct",
        "MIN_VOLUME_24H": "min_volume_24h",
        "STOP_ATR_MULTIPLIER": "stop_atr_multiplier",
        "STOP_FIXED_PCT": "stop_fixed_pct",
        "MAX_POSITION_SIZE_PCT": "max_position_size_pct",
    },
    "scripts.trade_executor": {
        "MAX_OPEN_POSITIONS": "max_positions",
        "MIN_RR": "min_rr",
    },
    "scripts.position_manager": {
        "TRAIL_ATR_MULTIPLIER": "trail_atr_multiplier",
        "MAX_HOLDING_CANDLES": "max_holding_candles",
        "TP1_SELL_PCT": "tp1_sell_pct",
        "TP2_SELL_PCT": "tp2_sell_pct",
        "TP3_SELL_PCT": "tp3_sell_pct",
    },
    "scripts.paper_trading_engine": {
        "INITIAL_BALANCE": "account_balance",
        "TAKER_FEE": "taker_fee",
        "MAKER_FEE": "maker_fee",
        "SLIPPAGE_BPS": "slippage_bps",
    },
}


class Pipeline:
    """Orchestrate sequential execution of trading pipeline stages.

    Accepts an optional ``container`` (ServiceContainer) for dependency
    injection.  When provided, stages use services from the container
    instead of the old module-level config override approach.
    """

    def __init__(self, config: AppConfig, logger: PipelineLogger,
                 container: Any = None) -> None:
        self.config = config
        self.logger = logger
        self.container = container
        self._notifier = None
        self.results: list[StageResult] = []

    def set_notifier(self, notifier: Any) -> None:
        """Set the centralized Notifier for notification dispatch."""
        self._notifier = notifier

    def run(self) -> list[StageResult]:
        """Run all stages in sequence. Returns list of ``StageResult``.

        If any stage fails, subsequent stages are skipped and the
        pipeline returns immediately.

        A ``DecisionTrace`` is recorded across all stages tracing the
        top-ranked candidate's journey — accepted, rejected, or skipped
        at each stage, with the specific reason and scores.
        """
        self.results = []
        self.logger.pipeline_start()
        self._apply_config()

        trace = DecisionTrace(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Use DI stage runners when container is available
        if self.container is not None:
            stages: list[tuple[str, Callable[[], Any], str]] = [
                ("Scanner",       self._run_scanner_di,  "data/scanner_results.json"),
                ("Decision",      self._run_decision_di, "data/decision_results.json"),
                ("Risk",          self._run_risk_di,      "data/risk_results.json"),
                ("Trade",         self._run_trade_di,     "data/trade_plan.json"),
                ("Position",      self._run_position_di,  "data/positions.json"),
                ("Paper",         self._run_paper_di,     "data/paper_orders.json"),
            ]
        else:
            stages: list[tuple[str, Callable[[], Any], str]] = [
                ("Scanner",       self._run_scanner,       "data/scanner_results.json"),
                ("Decision",      self._run_decision,      "data/decision_results.json"),
                ("Risk",          self._run_risk,           "data/risk_results.json"),
                ("Trade",         self._run_trade,          "data/trade_plan.json"),
                ("Position",      self._run_position,       "data/positions.json"),
                ("Paper",         self._run_paper,          "data/paper_orders.json"),
            ]

        for name, fn, output_file in stages:
            result = self._run_stage(name, fn, output_file)
            self.results.append(result)
            if result.success:
                self._trace_stage(name, output_file, trace)
            if not result.success:
                trace.save()
                self.logger.pipeline_end(
                    sum(r.duration for r in self.results)
                )
                return self.results

        trace.save()
        total_elapsed = sum(r.duration for r in self.results)
        self.logger.pipeline_end(total_elapsed)
        return self.results

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _run_stage(
        self, name: str, fn: Callable[[], Any], output_file: str,
    ) -> StageResult:
        self.logger.stage_start(name)
        t0 = time.time()

        def _captured_fn() -> None:
            with self.logger.capture_output():
                fn()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_captured_fn)
            future.result(timeout=STAGE_TIMEOUT)
            executor.shutdown(wait=True)
            elapsed = time.time() - t0
            detail = self._verify_output(output_file)
            self.logger.stage_done(name, detail)
            return StageResult(name=name, success=True, duration=elapsed, detail=detail)

        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            elapsed = time.time() - t0
            reason = f"Stage timed out after {STAGE_TIMEOUT}s"
            self.logger.stage_fail(name, reason)
            return StageResult(
                name=name, success=False, duration=elapsed, error=reason,
            )
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            elapsed = time.time() - t0
            reason = str(exc)
            self.logger.stage_fail(name, reason)
            return StageResult(
                name=name, success=False, duration=elapsed, error=reason,
            )

    @staticmethod
    def _verify_output(path: str) -> str:
        if not os.path.exists(path):
            return "no output"
        size = os.path.getsize(path)
        if size > 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    def _apply_config(self) -> None:
        """Override each module's module-level constants from AppConfig."""
        for module_name, attr_map in _CONFIG_OVERRIDES.items():
            try:
                mod = importlib.import_module(module_name)
            except ImportError:
                self.logger.info(f"Cannot import {module_name} — skipping config")
                continue

            for attr_name, config_key in attr_map.items():
                value = getattr(self.config, config_key, None)
                if value is not None and hasattr(mod, attr_name):
                    setattr(mod, attr_name, value)

    # ------------------------------------------------------------------
    #  Decision Trace — records why the top candidate was accepted /
    #  rejected at every pipeline stage.
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_stage(name: str, output_file: str, trace: DecisionTrace) -> None:
        """Read the stage output and append a trace entry for the top candidate."""
        if not os.path.exists(output_file):
            return

        try:
            with open(output_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        if name == "Scanner":
            pairs = data.get("pairs", [])
            if not pairs:
                return
            top = pairs[0]
            trace.top_candidate = top["symbol"]
            trace.add(
                "Scanner", top["symbol"], "ACCEPTED",
                f"Rank #{top.get('rank', 1)} overall={top.get('overall', 0):.1f} "
                f"signal={top.get('signal', 'N/A')}",
                {k: top[k] for k in (
                    "overall", "signal", "trend_score", "momentum_score",
                    "volume_score", "volatility_score", "liquidity_score",
                ) if k in top},
            )
        elif name == "Decision":
            decisions = data.get("decisions", [])
            for d in decisions:
                if d.get("symbol") == trace.top_candidate:
                    prob = d.get("probability", 0)
                    rec = d.get("recommendation", "N/A")
                    accepted = prob >= 35
                    trace.add(
                        "Decision", trace.top_candidate,
                        "ACCEPTED" if accepted else "REJECTED",
                        f"probability={prob:.1f} → {rec}"
                        f"{' (below IGNORE threshold)' if not accepted else ''}",
                        {k: d[k] for k in (
                            "probability", "recommendation", "trend_score",
                            "momentum_score", "volume_score", "volatility_score",
                            "risk_score", "reward_score", "expected_rr",
                            "overall_score",
                        ) if k in d},
                    )
                    return
            trace.add("Decision", trace.top_candidate, "SKIPPED",
                      "Symbol not found in decision results")
        elif name == "Risk":
            results = data.get("results", [])
            for r in results:
                if r.get("symbol") == trace.top_candidate:
                    approval = r.get("approval", "REJECTED")
                    reason = r.get("rejection_reason", "") or "All checks passed"
                    trace.add(
                        "Risk", trace.top_candidate, approval, reason,
                        {k: r[k] for k in (
                            "probability", "entry_price", "stop_loss",
                            "position_size", "position_value", "risk_percent",
                            "expected_rr", "stop_method", "risk_amount",
                        ) if k in r},
                    )
                    return
            trace.add("Risk", trace.top_candidate, "SKIPPED",
                      "Symbol not found in risk results")
        elif name == "Trade":
            plans = data.get("plans", [])
            for p in plans:
                if p.get("symbol") == trace.top_candidate:
                    status = p.get("status", "UNKNOWN")
                    conf = p.get("confidence", 0)
                    trace.add(
                        "Trade", trace.top_candidate,
                        "ACCEPTED" if status == "READY" else status,
                        f"confidence={conf:.1f} status={status}",
                        {k: p[k] for k in (
                            "confidence", "entry_price", "quantity",
                            "position_size_usdt", "probability",
                            "recommendation", "risk_reward",
                        ) if k in p},
                    )
                    return
            trace.add("Trade", trace.top_candidate, "SKIPPED",
                      "Symbol not found in trade plans")
        elif name == "Position":
            positions = data.get("positions", [])
            for p in positions:
                if p.get("symbol") == trace.top_candidate:
                    status = p.get("status", "UNKNOWN")
                    pnl = p.get("floating_pnl", 0)
                    pnl_str = f" PnL={pnl:+.2f}" if status == "OPEN" else ""
                    trace.add(
                        "Position", trace.top_candidate, status,
                        f"Position {status}{pnl_str}",
                        {k: p[k] for k in (
                            "status", "entry_price", "current_price",
                            "floating_pnl", "floating_pnl_pct",
                            "remaining_qty", "stop_loss",
                        ) if k in p},
                    )
                    return
            trace.add("Position", trace.top_candidate, "SKIPPED",
                      "Symbol not found in positions")
        elif name == "Paper":
            orders = data.get("orders", []) if isinstance(data, dict) else data
            for o in orders:
                if o.get("symbol") == trace.top_candidate:
                    status = o.get("status", "UNKNOWN")
                    side = o.get("side", "N/A")
                    fill = o.get("fill_price", o.get("entry_price", 0))
                    action = f"{side} {status} @ ${fill}" if status == "FILLED" else f"{side} {status}"
                    trace.add(
                        "Paper", trace.top_candidate, status, action,
                        {k: o[k] for k in (
                            "side", "status", "fill_price", "entry_price",
                            "quantity", "filled_quantity", "net_pnl",
                        ) if k in o},
                    )
                    return
            trace.add("Paper", trace.top_candidate, "SKIPPED",
                      "Symbol not found in paper orders")

    # ------------------------------------------------------------------
    #  Stage runners (delayed imports to avoid circular dependencies)
    #  When ``self.container`` is set, services are used instead.
    # ------------------------------------------------------------------

    @staticmethod
    def _run_scanner() -> None:
        from scripts import scanner
        scanner.main()
        _touch_file("data/scanner_results.json")

    def _run_scanner_di(self) -> None:
        self.container.scanner.run()
        _touch_file("data/scanner_results.json")

    @staticmethod
    def _run_decision() -> None:
        from scripts import decision_engine
        decision_engine.main()

    def _run_decision_di(self) -> None:
        from scripts import decision_engine

        engine = decision_engine.DecisionEngine()
        decisions = engine.run()

        report = decision_engine.DecisionReport()
        report.to_json(
                decisions,
                "data/decision_results.json"
        )

    @staticmethod
    def _run_risk() -> None:
        from scripts import risk_manager
        risk_manager.main()

    def _run_risk_di(self) -> None:
        from scripts import risk_manager

        if self.container is not None:
            wallet = self.container.wallet
            balance = wallet.balance
            if balance <= 0:
                balance = self.config.account_balance
        else:
            balance, _ = risk_manager._resolve_account_state()

        existing_exposure = risk_manager._existing_open_exposure()
        equity = balance + existing_exposure
        mm_config = risk_manager.MoneyManagementConfig(
            mode=risk_manager.MoneyManagementMode(
                risk_manager.MONEY_MANAGEMENT_MODE
            ),
        )
        manager = risk_manager.RiskManager(
            balance=balance,
            equity=equity,
            existing_exposure=existing_exposure,
            risk_per_trade=self.config.max_risk_per_trade_pct,
            max_daily_loss=self.config.max_daily_loss_pct,
            max_positions=self.config.max_positions,
            mm_config=mm_config,
        )
        results = manager.run()

        if results:
            risk_manager.RiskReport.to_csv(results, "data/risk_results.csv")
            risk_manager.RiskReport.to_json(results, "data/risk_results.json")

    @staticmethod
    def _run_trade() -> None:
        from scripts import trade_executor
        trade_executor.main()

    def _run_trade_di(self) -> None:
        from scripts import trade_executor

        equity = None
        if self.container is not None:
            equity = self.container.wallet.equity

        executor = trade_executor.TradeExecutor(equity=equity)
        plans = executor.run()

        trade_executor.PlanExport.to_csv(plans, "data/trade_plan.csv")
        trade_executor.PlanExport.to_json(plans, "data/trade_plan.json")

    @staticmethod
    def _run_position() -> None:
        from scripts import position_manager
        position_manager.main()

    def _run_position_di(self) -> None:
        orders = self.container.order.get_orders()
        # PositionManager still reads from JSON files internally
        from scripts import position_manager
        position_manager.main()

    @staticmethod
    def _run_paper() -> None:
        from scripts import paper_trading_engine
        paper_trading_engine.main()

    def _run_paper_di(self) -> None:
        from scripts import paper_trading_engine

        is_live = (
            self.container is not None
            and self.container.order.mode == "LIVE"
        )

        # Check safety guards — always run reconciliation (TP/SL, PnL),
        # but only allow new positions when the guard passes.
        allow_new = True
        if self.container is not None:
            ok, reason = self.container.safeguard.can_open_new_position()
            if not ok:
                self.logger.info(f"Pipeline paper new-order guard: {reason}")
                allow_new = False

        # In LIVE mode the paper engine runs for position tracking and
        # reconciliation only — never creates phantom paper positions.
        # Real positions are tracked on the exchange, not in the paper wallet.
        paper_allow_new = allow_new and not is_live
        paper_trading_engine.main(
            notifier=self._notifier,
            allow_new_positions=paper_allow_new,
        )

        # ── LIVE mode: submit real exchange orders ──────────────────
        # Independent of the paper engine wallet. Real orders go through
        # OrderManager which has its own safeguard check inside execute().
        if is_live and self.container.order.is_live_enabled():
            self._execute_live_plans()

    def _execute_live_plans(self) -> None:
        """Read READY plans from trade_plan.json and submit real orders."""
        import json, os  # noqa: PLC0415

        path = "data/trade_plan.json"
        if not os.path.exists(path):
            return

        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        plans = [p for p in data.get("plans", []) if p.get("status") == "READY"]
        if not plans:
            return

        order_mgr = self.container.order
        for plan in plans:
            symbol = plan.get("symbol", "")
            if not symbol:
                continue

            self.logger.info(
                f"LIVE execution: submitting BUY for {symbol} "
                f"${plan.get('position_size_usdt', 0):,.2f}"
            )
            try:
                result = order_mgr.execute(plan)
                status = result.get("status", "?") if isinstance(result, dict) else getattr(result, "status", "?")
                self.logger.info(
                    f"LIVE execution result for {symbol}: {status}"
                )
            except Exception as exc:
                self.logger.error(
                    f"LIVE execution failed for {symbol}: {exc}"
                )
