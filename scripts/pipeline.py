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
from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES
from scripts.paper_state_lock import paper_state_writes, merge_positions


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
    "scripts.execution_provider": {
        "PAPER_INITIAL_BALANCE": "account_balance",
        "QUOTE_CURRENCY": "quote_currency",
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
        self._resync_live_positions()

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

    def _resync_live_positions(self) -> None:
        """Refresh ``data/live_positions.json`` from the real exchange
        balance before this pipeline run starts.

        Without this, the cache is only ever touched by
        ``OrderManager`` right after the BOT's own BUY/SELL fills — a
        position closed manually on the exchange (outside the bot)
        never gets removed from the cache. That stale entry then keeps
        counting against ``max_positions`` in ``risk_manager.py``
        forever (via ``_count_open_positions()``, which reads this
        same file), silently blocking every future BUY even though the
        position is long gone. Doing a full resync here — once per
        pipeline cycle, before Risk ever counts anything — is what
        actually keeps the count honest.

        PAPER mode: no-op (paper positions aren't exchange balances).
        Any failure here is logged and swallowed — a resync problem
        must never block the pipeline itself; the existing stage-level
        guards already handle a temporarily-stale cache safely.
        """
        try:
            paper_mode = bool(getattr(self.config, "paper_mode", True))
        except Exception:
            paper_mode = True
        if paper_mode:
            return

        try:
            if self.container is not None:
                exchange = self.container.exchange
                quote = getattr(self.container.config, "quote_currency", "USDT")
            else:
                from scripts.exchange_manager import ExchangeManager  # noqa: PLC0415

                exchange = ExchangeManager(
                    active=getattr(self.config, "exchange", "binance"),
                    api_key=getattr(self.config, "api_key", ""),
                    api_secret=getattr(self.config, "api_secret", ""),
                )
                quote = getattr(self.config, "quote_currency", "USDT")

            from scripts.live_position_sync import (  # noqa: PLC0415
                LivePositionSync,
                load_live_positions,
                merge_live_positions,
            )

            syncer = LivePositionSync(exchange, quote_currency=quote)
            fresh = syncer.sync_all_positions()

            # Union of what's cached now + what came back fresh — so a
            # position that fell out of the balance response entirely
            # (fully sold, zero balance no longer even listed) still
            # gets purged, not just ones that came back with dust.
            previously_cached = set(load_live_positions().keys())
            fresh_symbols = {p["symbol"] for p in fresh}
            synced_symbols = list(previously_cached | fresh_symbols)

            merge_live_positions(fresh, synced_symbols=synced_symbols)
            self.logger.info(
                f"Live position resync: {len(fresh)} open position(s) confirmed "
                "against exchange balance."
            )
        except Exception as exc:
            self.logger.info(f"Live position resync failed (non-fatal): {exc}")

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
                    qc = os.getenv("QUOTE_CURRENCY", "USDT").upper()
                    action = f"{side} {status} @ {fill} {qc}" if status == "FILLED" else f"{side} {status}"
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
        """Unified execution stage for both PAPER and LIVE modes.

        Uses ExecutionPipeline for shared business logic.
        The ExecutionProvider implementation (Paper vs Live) is the
        only difference — everything else is identical.
        """
        qc = (getattr(self.config, "quote_currency", None) or os.getenv("QUOTE_CURRENCY", "USDT")).upper()
        from scripts.execution_provider import (
            PaperExecutionProvider,
            LiveExecutionProvider,
            create_execution_provider,
        )
        from scripts.execution_pipeline import ExecutionPipeline

        is_live = (
            self.container is not None
            and self.container.order.mode == "LIVE"
            # Only an ARMED live session may use the real exchange. A LIVE
            # configured but not-armed run degrades to the paper provider
            # (mirrors ExecutionEngine's simulation fallback) so that no
            # real order can ever leave through this stage before the
            # operator ran /golive + CONFIRM LIVE.
            and self.container.order.is_live_enabled()
        )

        allow_new = True
        if self.container is not None:
            ok, reason = self.container.safeguard.can_open_new_position()
            if not ok:
                self.logger.info(f"Pipeline new-order guard: {reason}")
                allow_new = False

        # Create the right provider for the mode
        mode = "LIVE" if is_live else "PAPER"
        if is_live:
            provider = LiveExecutionProvider(
                self.container.exchange,
                self.config,
            )
        else:
            provider = PaperExecutionProvider()

        pipeline = ExecutionPipeline(
            provider,
            quote_currency=getattr(self.config, "quote_currency", "USDT"),
        )

        # Read READY plans
        import json, os  # noqa: PLC0415

        plan_path = "data/trade_plan.json"
        ready_plans: list[dict[str, Any]] = []
        if os.path.exists(plan_path):
            try:
                with open(plan_path) as f:
                    plan_data = json.load(f)
                ready_plans = [
                    p for p in plan_data.get("plans", [])
                    if p.get("status") == "READY"
                ]
            except (json.JSONDecodeError, OSError):
                pass

        # --- Unified BUY execution (paper or live) ---
        if allow_new:
            for plan in ready_plans:
                symbol = plan.get("symbol", "")
                if not symbol:
                    continue

                self.logger.info(
                    f"{mode} execution: submitting BUY for {symbol} "
                    f"{plan.get('position_size_usdt', 0):,.2f} {qc}"
                )
                try:
                    result = pipeline.execute_plan(plan)
                    if result is not None:
                        status = result.status
                        self.logger.info(
                            f"{mode} execution result for {symbol}: {status}"
                        )
                        if status == "FILLED" and self._notifier is not None:
                            try:
                                self._notifier.notify_buy_opened(
                                    symbol=symbol,
                                    exchange=getattr(self.config, "exchange", ""),
                                    timeframe=getattr(self.config, "timeframe", ""),
                                    entry_price=plan.get("entry_price", 0),
                                    quantity=plan.get("quantity", 0),
                                    position_size=plan.get("position_size_usdt", 0),
                                    stop_loss=plan.get("stop_loss", 0),
                                    take_profit=plan.get("tp1", 0),
                                    take_profit_2=plan.get("tp2", 0),
                                    take_profit_3=plan.get("tp3", 0),
                                    reasons=["Pipeline execution"],
                                )
                                try:
                                    notified_path = "data/.notified_buys"
                                    notified = set()
                                    if os.path.exists(notified_path):
                                        with open(notified_path) as nf:
                                            notified = set(line.strip() for line in nf if line.strip())
                                    notified.add(symbol)
                                    with open(notified_path, "w") as nf:
                                        for sym in sorted(notified):
                                            nf.write(f"{sym}\n")
                                except Exception:
                                    pass
                            except Exception:
                                self.logger.warning(f"BUY notification failed for {symbol}")
                except Exception as exc:
                    self.logger.error(
                        f"{mode} execution failed for {symbol}: {exc}"
                    )

        # --- Unified TP/SL reconciliation (paper or live) ---
        self._reconcile_positions(pipeline, provider, is_live)

        # --- Persist paper state (for Telegram / reporting) ---
        if not is_live:
            self._persist_paper_state(provider)

    def _reconcile_positions(
        self,
        pipeline: Any,
        provider: Any,
        is_live: bool,
    ) -> None:
        """Reconcile all open positions — shared TP/SL logic for both modes.

        LIVE mode runs through ``scripts.exit_gate`` (per-symbol lock +
        atomic positions.json read-modify-write) so that no two exit
        paths can sell the same quantity, and protection orders are
        cancelled BEFORE the market sell. PAPER mode keeps its existing
        single-threaded batched behavior untouched.
        """
        if is_live:
            self._reconcile_positions_live(pipeline)
            return

        qc = (getattr(self.config, "quote_currency", None) or os.getenv("QUOTE_CURRENCY", "USDT")).upper()
        import json, os  # noqa: PLC0415

        pos_path = "data/positions.json"
        if not os.path.exists(pos_path):
            return

        try:
            with open(pos_path) as f:
                pos_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        positions = pos_data.get("positions", [])
        if not positions:
            return

        # Load plans for TP/SL reference prices
        plan_path = "data/trade_plan.json"
        plans_by_symbol: dict[str, dict] = {}
        if os.path.exists(plan_path):
            try:
                with open(plan_path) as f:
                    plan_data = json.load(f)
                for p in plan_data.get("plans", []):
                    plans_by_symbol[p["symbol"]] = p
            except (json.JSONDecodeError, OSError):
                pass

        # Fetch current prices for open positions
        open_positions = [p for p in positions if p.get("status") in (
            "OPEN", "PARTIAL", "BREAKEVEN", "TRAILING",
        )]

        if not open_positions:
            return

        # Fetch prices from the configured exchange (indodax pairs like
        # GOAT/IDR never resolve on binance).
        try:
            from bot.data import build_public_exchange  # noqa: PLC0415
            exchange = build_public_exchange(getattr(self.config, "exchange", "binance"))
            symbols = [p["symbol"] for p in open_positions if "symbol" in p]
            tickers = exchange.fetch_tickers(symbols) if symbols else {}
        except Exception as exc:
            self.logger.debug(f"Reconciliation ticker fetch failed: {exc}")
            return

        updated_positions = []
        for pos in positions:
            sym = pos.get("symbol", "")
            if pos.get("status") not in OPEN_STATUSES:
                updated_positions.append(pos)
                continue

            ticker = tickers.get(sym) if sym in tickers else None
            current_price = None
            if ticker is not None:
                current_price = float(ticker.get("last", 0) or 0)

            if current_price is None or current_price <= 0:
                updated_positions.append(pos)
                continue

            plan = plans_by_symbol.get(sym, {})
            reconciled = pipeline.reconcile_position(
                sym, current_price, pos, plan=plan,
            )
            if reconciled is not None:
                updated_positions.append(reconciled)
                old_status = pos.get("status")
                new_status = reconciled.get("status")
                if old_status != new_status and new_status in CLOSED_STATUSES:
                    self.logger.info(
                        f"Position {sym}: {old_status} → {new_status} "
                        f"(PnL: {reconciled.get('total_pnl', 0):+.2f} {qc})"
                    )
            else:
                updated_positions.append(pos)

        # Write updated positions (atomic merge so a concurrent writer's
        # symbols are preserved — BUG-4).
        merge_positions(updated_positions)

    def _reconcile_positions_live(self, pipeline: Any) -> None:
        """LIVE TP/SL reconciliation — serialized per symbol via exit_gate.

        Every LIVE exit path (monitor, pipeline, protection scheduler,
        Telegram /sell) shares the same per-symbol lock and the same
        atomic positions.json read-modify-write, so two threads can
        never both sell the same quantity. Protection orders are
        cancelled BEFORE the market sell (inside ``reconcile_exit``)
        whenever an exit is about to fire.
        """
        from scripts.exit_gate import reconcile_exit  # noqa: PLC0415
        import json, os  # noqa: PLC0415

        qc = (getattr(self.config, "quote_currency", None) or os.getenv("QUOTE_CURRENCY", "USDT")).upper()

        pos_path = "data/positions.json"
        if not os.path.exists(pos_path):
            return

        try:
            with open(pos_path) as f:
                pos_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        positions = pos_data.get("positions", [])
        open_positions = [
            p for p in positions if p.get("status") in OPEN_STATUSES
        ]
        if not open_positions:
            return

        # Load plans for TP/SL reference prices
        plan_path = "data/trade_plan.json"
        plans_by_symbol: dict[str, dict] = {}
        if os.path.exists(plan_path):
            try:
                with open(plan_path) as f:
                    plan_data = json.load(f)
                for p in plan_data.get("plans", []):
                    plans_by_symbol[p["symbol"]] = p
            except (json.JSONDecodeError, OSError):
                pass

        # Fetch current prices from the configured exchange
        try:
            from bot.data import build_public_exchange  # noqa: PLC0415
            exchange = build_public_exchange(getattr(self.config, "exchange", "binance"))
            symbols = [p["symbol"] for p in open_positions if "symbol" in p]
            tickers = exchange.fetch_tickers(symbols) if symbols else {}
        except Exception as exc:
            self.logger.debug(f"Reconciliation ticker fetch failed: {exc}")
            return

        for pos in positions:
            sym = pos.get("symbol", "")
            if pos.get("status") not in OPEN_STATUSES:
                continue

            ticker = tickers.get(sym) if sym in tickers else None
            if ticker is None:
                continue
            current_price = float(ticker.get("last", 0) or 0)
            if current_price <= 0:
                continue

            plan = plans_by_symbol.get(sym, {})

            def _on_reconciled(
                prev: dict,
                reconciled: Any,
                _sym: str = sym,
            ) -> None:
                if reconciled is None:
                    return
                old_status = prev.get("status")
                new_status = reconciled.get("status")
                if old_status != new_status and new_status in CLOSED_STATUSES:
                    self.logger.info(
                        f"Position {_sym}: {old_status} → {new_status} "
                        f"(PnL: {reconciled.get('total_pnl', 0):+.2f} {qc})"
                    )

            reconcile_exit(
                pipeline,
                sym,
                current_price,
                plan,
                cancel_protection=self._cancel_live_protection,
                on_reconciled=_on_reconciled,
            )

    def _cancel_live_protection(self, symbol: str) -> None:
        """Cancel live protection orders for a position that just closed."""
        try:
            from scripts.protection_manager import ProtectionManager
            pm = ProtectionManager(
                self.container.exchange,
                getattr(self.container, "_config", None),
            )
            pm.cancel_protection(symbol, reason="pipeline_reconciliation")
        except Exception as exc:
            self.logger.debug(f"Cancel protection for {symbol}: {exc}")

    @paper_state_writes
    def _persist_paper_state(self, provider: Any) -> None:
        """Persist paper provider state to balance/orders JSON files."""
        import json  # noqa: PLC0415

        balance = provider.get_balance()
        bal_path = "data/paper_balance.json"
        try:
            with open(bal_path) as f:
                pb = json.load(f)
            pb["final_balance"] = round(balance, 2)
            pb["final_equity"] = round(balance, 2)
            with open(bal_path, "w") as f:
                json.dump(pb, f, indent=2)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pb = {
                "initial_balance": self.config.account_balance,
                "final_balance": round(balance, 2),
                "final_equity": round(balance, 2),
            }
            try:
                with open(bal_path, "w") as f:
                    json.dump(pb, f, indent=2)
            except OSError:
                pass
