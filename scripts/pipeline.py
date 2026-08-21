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
from scripts.position_status import OPEN_STATUSES, CLOSED_STATUSES, is_open
from scripts.paper_state_lock import (
    add_notified_buy,
    merge_positions,
    paper_state_writes,
)


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
        "TIMEFRAME": "timeframe",
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
        "MIN_STOP_PCT": "min_stop_pct",
        "MIN_TP1_PCT": "min_tp1_pct",
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

            exclude = getattr(self.config, "exclude_symbols", "") or ""
            syncer = LivePositionSync(exchange, quote_currency=quote, exclude_symbols=exclude)
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
                # A zero/empty balance read is NOT the same as a legit
                # empty paper account once trading has begun — sizing risk
                # against the ACCOUNT_BALANCE constant on stale capital
                # silently drifts exposure. Log it loudly so the operator
                # notices, and let the exchange-side preflights still gate.
                self.logger.warning(
                    "Risk stage: balance read returned <= 0 (%.2f) — "
                    "falling back to ACCOUNT_BALANCE (%.2f). Verify the "
                    "account state file is being written.",
                    balance, self.config.account_balance,
                )
                balance = self.config.account_balance
        else:
            balance, _ = risk_manager._resolve_account_state()

        existing_exposure = risk_manager._existing_open_exposure()
        equity = balance + existing_exposure
        mm_config = risk_manager.MoneyManagementConfig(
            mode=risk_manager.MoneyManagementMode(
                self.config.money_management_mode
            ),
            risk_per_trade=self.config.max_risk_per_trade_pct / 100.0,
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
            max_position_pct_of_balance=self.config.max_position_size_pct,
        )
        manager = risk_manager.RiskManager(
            balance=balance,
            equity=equity,
            existing_exposure=existing_exposure,
            risk_per_trade=self.config.max_risk_per_trade_pct,
            max_daily_loss=self.config.max_daily_loss_pct,
            max_positions=self.config.max_positions,
            max_position_size_pct=self.config.max_position_size_pct,
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
        if equity is not None:
            # Scale the concurrent-position cap with equity, matching
            # the risk stage, instead of the fixed MAX_POSITIONS env
            # value — otherwise trade planning would re-clamp back
            # down to 1 even after the risk stage approved more.
            executor.validator.max_positions = (
                trade_executor.dynamic_max_positions(equity)
            )
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

        # Read READY plans (before the new-order guard so their symbols can
        # be excluded from the max-open-positions count — the Position stage
        # already simulated these into positions.json as OPEN, and counting
        # them as blocking open positions would reject the very BUY that is
        # about to execute them).
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

        allow_new = True
        if self.container is not None:
            planned_symbols = {
                p.get("symbol", "") for p in ready_plans if p.get("symbol")
            }
            self.container.safeguard.set_planned_symbols(planned_symbols)
            ok, reason = self.container.safeguard.can_open_new_position()
            if not ok:
                self.logger.info(f"Pipeline new-order guard: {reason}")
                allow_new = False

        filled_symbols: set[str] = set()
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
            notifier=self._notifier,
        )

        # --- Unified BUY execution (paper or live) ---
        min_notional = float(
            os.getenv(
                "MIN_ORDER_NOTIONAL",
                "10000" if qc == "IDR" else "10",
            )
        )
        open_held_bases: set[str] = set()
        try:
            # Symbols about to be bought THIS cycle are excluded: the
            # Position stage simulates their OPEN entry in positions.json
            # BEFORE execution, so they must not count as "already held"
            # (that would make the pipeline skip every buy). Only genuinely
            # held symbols — confirmed by the exchange sync
            # (live_positions.json) or by older managed positions — block
            # a re-entry.
            _plan_bases = {
                str(p.get("symbol", "")).split("/")[0].upper()
                for p in ready_plans if p.get("symbol")
            }
            for src in ("positions.json", "live_positions.json"):
                _data = json.load(open(src)) if os.path.exists(src) else {}
                if src == "positions.json":
                    _items = _data.get("positions", []) if isinstance(_data, dict) else []
                else:
                    _items = list(_data.values()) if isinstance(_data, dict) else []
                for _p in _items:
                    if not is_open(_p.get("status")):
                        continue
                    _b = str(_p.get("symbol", "")).split("/")[0].upper()
                    if _b and _b not in _plan_bases:
                        open_held_bases.add(_b)
        except Exception:
            pass
        if allow_new:
            for plan in ready_plans:
                symbol = plan.get("symbol", "")
                if not symbol:
                    continue

                plan_size = float(plan.get("position_size_usdt", 0) or 0)
                if plan_size < min_notional:
                    self.logger.info(
                        f"{mode} execution: SKIP {symbol} order size "
                        f"{plan_size:,.0f} {qc} < exchange minimum "
                        f"{min_notional:,.0f} {qc} (MIN_ORDER_NOTIONAL)"
                    )
                    continue

                held_base = symbol.split("/")[0].upper()
                if held_base in open_held_bases:
                    self.logger.info(
                        f"{mode} execution: SKIP {symbol} — already holding "
                        f"{held_base} (no double positions allowed)"
                    )
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
                        if status == "FILLED":
                            filled_symbols.add(symbol)
                            if self._notifier is not None:
                                try:
                                    sent = self._notifier.notify_buy_opened(
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
                                    if sent:
                                        # Only record as notified when delivery
                                        # actually succeeded, otherwise the
                                        # BUY_OPENED is retried on the next
                                        # restart instead of being lost.
                                        # Serialized + atomic via
                                        # scripts/paper_state_lock so a
                                        # concurrent writer (startup daemon
                                        # thread — BUG-4) can never clobber this
                                        # entry.
                                        try:
                                            add_notified_buy(symbol)
                                        except Exception:
                                            pass
                                except Exception:
                                    self.logger.warning(f"BUY notification failed for {symbol}")
                except Exception as exc:
                    self.logger.error(
                        f"{mode} execution failed for {symbol}: {exc}"
                    )

        # --- Unified TP/SL reconciliation (paper or live) ---
        live_unarmed = (
            not is_live
            and self.container is not None
            and getattr(self.container.order, "mode", None) == "LIVE"
        )
        if is_live:
            self._merge_live_positions_into_managed()
        if live_unarmed:
            # LIVE configured but NOT armed: positions.json holds REAL
            # exchange holdings (restored at startup / adopted), which the
            # paper provider would SIMULATE-sell and mark STOPPED/CLOSED
            # without any order leaving the exchange — and ghost pruning
            # would CLOSE real holdings that simply are not in the paper
            # ledger. Defer ALL position bookkeeping until the operator
            # runs /golive + CONFIRM LIVE; only then may exits happen.
            self.logger.info(
                "LIVE mode configured but not armed: TP/SL reconciliation "
                "deferred until /golive (no simulated exits)"
            )
        else:
            self._reconcile_positions(pipeline, provider, is_live)

        # --- Persist paper state (for Telegram / reporting) ---
        if not is_live and not live_unarmed:
            self._persist_paper_state(provider)
            # Clean simulated-but-never-executed positions out of
            # positions.json: any OPEN entry with no OPEN counterpart in
            # the authoritative paper ledger is a ghost (the Position
            # stage simulated it, execution rejected it). Ghosts must
            # not keep producing BUY_OPENED notifications, inflating
            # equity/exposure, or blocking new buys (BUG-4).
            self._prune_ghost_positions(provider)
        else:
            # LIVE mode: the Position stage simulates every READY plan
            # into positions.json as OPEN *before* the real BUY runs. A
            # rejected BUY (invalid pair, maintenance, blacklist) would
            # otherwise leave an OPEN ghost that inflates open-position
            # counts and makes TP/SL reconciliation chase balances that
            # do not exist. live_positions.json is exchange-confirmed
            # truth, so prune OPEN entries with no counterpart there.
            self._prune_live_ghost_positions(
                filled_symbols,
                exchange_manager=getattr(self.container, "exchange", None),
            )

    def _prune_ghost_positions(self, provider: Any) -> None:
        """Close positions.json OPEN entries not backed by paper_state.

        paper_state.json is the authoritative ledger of executed trades.
        positions.json is written by the Position stage from READY plans
        BEFORE execution; if the paper stage then rejects/skips a plan
        (guard, insufficient balance, missing price), the simulated OPEN
        entry would otherwise linger forever as a ghost.
        """
        import json, os  # noqa: PLC0415

        try:
            with open("data/paper_state.json") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        ledger_positions = state.get("positions") or {}

        pos_path = "data/positions.json"
        try:
            with open(pos_path) as f:
                pos_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

        pruned = False
        for p in pos_data.get("positions", []):
            if not is_open(p.get("status")):
                continue
            sym = p.get("symbol", "")
            vp = ledger_positions.get(sym)
            if vp is not None and vp.get("status") in OPEN_STATUSES:
                continue
            self.logger.warning(
                f"Pruning ghost position {sym} from positions.json "
                "(not present as OPEN in paper_state.json — simulated but "
                "never executed)"
            )
            p["status"] = "CLOSED"
            p["remaining_qty"] = 0.0
            p["unrealized_pnl"] = 0.0
            pruned = True

        if pruned:
            from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
            merge_positions(pos_data.get("positions", []))

    def _merge_live_positions_into_managed(self) -> None:
        """Track exchange-confirmed holdings in positions.json so they are
        managed by the same TP/SL reconcile as plan-derived positions.

        Without this a holding that has no plan entry (a BUY whose record
        was wrongly pruned, a position bought before arming, or a fill the
        strategy no longer signals) stays invisible to TP/SL management and
        to ``/positions`` forever. EXCLUDE_SYMBOLS coins and mismatched-
        quote symbols are never adopted. Existing managed entries win.
        """
        import json, os  # noqa: PLC0415

        pos_path = "data/positions.json"
        try:
            with open(pos_path) as f:
                pos_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pos_data = {}

        try:
            with open("data/live_positions.json") as f:
                live = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(live, dict) or not live:
            return

        try:
            from scripts.live_position_sync import parse_exclude_symbols  # noqa: PLC0415
            excluded = parse_exclude_symbols(os.getenv("EXCLUDE_SYMBOLS", ""))
        except Exception:
            excluded = set()

        qc = (getattr(self.config, "quote_currency", None)
              or os.getenv("QUOTE_CURRENCY", "USDT")).upper()

        managed = pos_data.get("positions", [])
        managed_by_sym = {p.get("symbol"): p for p in managed}

        # Generic SL/TP restore source for ANY adopted/healed symbol: the
        # write-once entry snapshot store keeps the plan levels (stop_loss,
        # tp1..3) as of each BUY fill — never symbol-specific.
        try:
            from scripts.live_position_sync import (  # noqa: PLC0415
                snapshot_levels_for_symbol,
            )
            _snapshot_levels = {
                sym: snapshot_levels_for_symbol(sym)
                for sym in live.keys() if isinstance(sym, str)
            }
        except Exception:
            _snapshot_levels = {}

        adopted = []
        # Generic SL/TP restoration for ANY adopted symbol: prefer the
        # live cache levels (stamped at buy time by
        # ExecutionPipeline.execute_plan), fall back to the write-once
        # entry snapshot. Never symbol-specific.
        def _level(sym: str, p: dict, key: str) -> float:
            val = float(p.get(key) or 0)
            if val <= 0:
                val = float(
                    _snapshot_levels.get(sym, {}).get(key, 0) or 0
                )
            return val

        for sym, p in live.items():
            if not isinstance(p, dict) or not sym:
                continue
            qty = float(p.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            base = sym.split("/")[0].upper()
            if base in excluded:
                continue
            sym_quote = sym.split("/")[1].upper() if "/" in sym else ""
            if sym_quote and sym_quote != qc:
                continue
            if sym in managed_by_sym:
                # A symbol the exchange STILL holds but the managed record
                # marks CLOSED/STOPPED is a SIMULATED exit (e.g. the
                # pipeline ran a stop-loss through the simulation executor
                # before /golive was ever called — no real sell happened).
                # The live cache only holds symbols with a real balance, so
                # a live record + non-OPEN managed record = still held on
                # the exchange. Re-adopt it as OPEN so the real TP/SL
                # reconciliation manages (and can actually sell) it.
                existing = managed_by_sym[sym]
                if (existing.get("status") or "OPEN") not in OPEN_STATUSES:
                    # A symbol the exchange STILL holds but the managed
                    # record marks CLOSED/STOPPED is a SIMULATED exit (e.g.
                    # the pipeline ran a stop-loss through the simulation
                    # executor before /golive was ever called — no real sell
                    # happened). The live cache only holds symbols with a
                    # real balance, so a live record + non-OPEN managed
                    # record = still held on the exchange. Drop the stale
                    # managed record and fall through to the normal adoption
                    # path below, which re-adds it as OPEN with restored
                    # SL/TP levels so the real reconciliation can sell it.
                    self.logger.warning(
                        f"Re-adopting {sym} as OPEN: managed record was "
                        f"{existing.get('status')} but the exchange still "
                        f"holds {qty} (simulated exit never sold)"
                    )
                    managed = [m for m in managed if m.get("symbol") != sym]
                    managed_by_sym = {p.get("symbol"): p for p in managed}
                    entry = p.get("entry_price")
                    if entry is None or float(entry) <= 0:
                        continue
                else:
                    # Already managed: never let a level-less exchange sync
                    # zero out the plan's stop/TP, and self-heal the case
                    # where an earlier buggy sync already did (managed record
                    # lost its stop but the live cache still knows it, or the
                    # write-once entry snapshot still has the plan levels).
                    existing = managed_by_sym[sym]
                    healed = False
                    for key in ("stop_loss", "tp1", "tp2", "tp3"):
                        if float(existing.get(key) or 0) <= 0:
                            val = float(p.get(key) or 0)
                            if val <= 0:
                                val = float(
                                    _snapshot_levels.get(sym, {}).get(key, 0) or 0
                                )
                            if val > 0:
                                existing[key] = val
                                healed = True
                    if float(existing.get("entry_price") or 0) <= 0 \
                            and float(p.get("entry_price") or 0) > 0:
                        existing["entry_price"] = float(p["entry_price"])
                        healed = True
                    if healed:
                        from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
                        merge_positions(managed)
                    continue
            entry = p.get("entry_price")
            if entry is None or float(entry) <= 0:
                # Unknown entry = NOT bot-managed. Adopting it with the
                # current price as a fake entry (stop 0 / TP 0) would
                # create an unprotected, unmanageable OPEN record that
                # counts against MAX_POSITIONS and blocks every future
                # buy. Leave it in live_positions.json only — /positions
                # still shows it, but nothing auto-trades it.
                continue

            # TP-slice basis: the ORIGINAL filled quantity, not the
            # current balance. After a restart the sync only knows the
            # balance (already reduced by TP1), so adopting
            # quantity=balance would make every TP sell 30% of the
            # shrunken remainder — tiny fills, and TP2/TP3 never reached
            # (GPS/IDR bug). The buy-time stamp survives restarts via
            # merge_live_positions; fall back to the synced qty only when
            # there is genuinely no known original size.
            tp_basis = float(p.get("original_quantity") or 0) or qty

            adopted.append({
                "symbol": sym,
                "entry_price": float(entry),
                "current_price": float(p.get("current_price") or entry),
                "quantity": tp_basis,
                "remaining_qty": float(p.get("remaining_qty") or tp_basis),
                "remaining_pct": 100.0,
                "cost_basis": float(entry) * tp_basis,
                "stop_loss": _level(sym, p, "stop_loss"),
                "current_stop": _level(sym, p, "stop_loss"),
                "tp1": _level(sym, p, "tp1"),
                "tp2": _level(sym, p, "tp2"),
                "tp3": _level(sym, p, "tp3"),
                "tp1_hit": bool(p.get("tp1_hit", False)),
                "tp2_hit": bool(p.get("tp2_hit", False)),
                "tp3_hit": bool(p.get("tp3_hit", False)),
                "floating_pnl": 0.0,
                "floating_pnl_pct": 0.0,
                "realized_pnl": 0.0,
                "total_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "status": "OPEN",
                "entry_time": p.get("synced_at") or p.get("entry_time", ""),
                "exchange": p.get("exchange", ""),
                "source": p.get("source", "live_exchange_adoption"),
            })

        if adopted:
            managed.extend(adopted)
            from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
            merge_positions(managed)
            self.logger.info(
                f"Adopted {len(adopted)} exchange-held position(s) into managed set: "
                f"{', '.join(a['symbol'] for a in adopted)}"
            )

    def _prune_live_ghost_positions(
        self,
        recently_filled: set[str] | None = None,
        exchange_manager: Any = None,
    ) -> None:
        """Close positions.json OPEN entries not backed by the exchange.

        The Position stage simulates every READY plan into positions.json
        as OPEN before the real BUY is submitted. If that BUY is then
        rejected (invalid pair, maintenance, blacklist) the simulated
        entry would linger as a ghost: it inflates open-position counts,
        makes TP/SL reconciliation chase balances that do not exist
        (``SL sell skipped: NO_BALANCE``), and poisons HEALTH/equity.
        ``live_positions.json`` is exchange-confirmed truth, so any OPEN
        entry without an OPEN counterpart there is a ghost and must be
        closed. EXCLUDE_SYMBOLS holdings (the operator's own coins) are
        never touched.

        CRITICAL: this runs in the SAME cycle as the BUY, so
        ``live_positions.json`` (resynced at the START of the cycle, i.e.
        BEFORE the BUY) is already stale for a position that JUST filled.
        Pruning against it destroyed a real filled KOMA position 2s after
        a successful BUY (2026-08-15): the coins remained on the exchange
        but positions.json showed CLOSED, so the position was never
        managed/sold and the strategy re-signalled the same symbol. Two
        guards therefore protect recently-created positions:
          1. symbols that FILLED in this cycle are never pruned;
          2. the live exchange balance is checked fresh — a position whose
             base asset still has a non-zero free balance is NOT a ghost.
        """
        import json, os  # noqa: PLC0415

        recently_filled = recently_filled or set()
        pos_path = "data/positions.json"
        if not os.path.exists(pos_path):
            return
        try:
            with open(pos_path) as f:
                pos_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        live = {}
        try:
            with open("data/live_positions.json") as f:
                live = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            live = {}

        try:
            from scripts.live_position_sync import parse_exclude_symbols  # noqa: PLC0415
            excluded = parse_exclude_symbols(os.getenv("EXCLUDE_SYMBOLS", ""))
        except Exception:
            excluded = set()

        held_on_exchange: set[str] = set()
        if exchange_manager is not None:
            try:
                _bal = exchange_manager.get_provider().fetch_balance()
                _free = _bal.get("free", _bal) if isinstance(_bal, dict) else {}
                for _coin, _qty in (_free or {}).items():
                    try:
                        if float(_qty or 0) > 0:
                            held_on_exchange.add(str(_coin).upper())
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                self.logger.debug(f"Ghost-prune balance check failed: {exc}")

        pruned = False
        for p in pos_data.get("positions", []):
            if not is_open(p.get("status")):
                continue
            sym = p.get("symbol", "")
            base = sym.split("/")[0].upper()
            if base in excluded:
                continue
            if sym in live or base in live:
                continue
            if sym in recently_filled or base in recently_filled:
                continue
            if base in held_on_exchange:
                continue
            self.logger.warning(
                f"Pruning live ghost position {sym} from positions.json "
                "(no matching balance on the exchange — BUY was rejected "
                "or the position was closed manually)"
            )
            p["status"] = "CLOSED"
            p["remaining_qty"] = 0.0
            p["unrealized_pnl"] = 0.0
            pruned = True

        if pruned:
            from scripts.paper_state_lock import merge_positions  # noqa: PLC0415
            merge_positions(pos_data.get("positions", []))

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
        # GOAT/IDR never resolve on binance). Shared cached client + TTL
        # ticker cache so we never burst the exchange with concurrent
        # /api/pairs calls (429 rate-limit fix).
        try:
            from bot.data import fetch_tickers_cached  # noqa: PLC0415
            symbols = [p["symbol"] for p in open_positions if "symbol" in p]
            tickers = fetch_tickers_cached(
                getattr(self.config, "exchange", "binance"), symbols,
            ) if symbols else {}
        except Exception as exc:
            self.logger.debug(f"Reconciliation ticker fetch failed: {exc}")
            return

        updated_positions = []
        for pos in positions:
            sym = pos.get("symbol", "")
            if pos.get("status") not in OPEN_STATUSES:
                updated_positions.append(pos)
                continue

            qty = float(pos.get("quantity", 0) or 0)
            rem = float(pos.get("remaining_qty", qty) or qty)
            if qty <= 0 or rem <= 0:
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
                    if not pos.get("closure_notified", False) and self._notifier is not None:
                        try:
                            from datetime import datetime, timedelta
                            entry_time = reconciled.get("entry_time") or reconciled.get("opened_at", "")
                            holding = timedelta()
                            if entry_time:
                                try:
                                    dt = datetime.fromisoformat(entry_time.split("+")[0].split("Z")[0])
                                    holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
                                    if holding.total_seconds() < 0:
                                        holding = timedelta()
                                except (ValueError, TypeError):
                                    pass
                            exit_reason = (
                                "Stop Loss" if new_status == "STOPPED"
                                else "Take Profit" if new_status == "CLOSED"
                                else "Strategy Exit"
                            )
                            self._notifier.notify_position_closed(
                                symbol=sym,
                                entry_price=reconciled.get("entry_price", 0),
                                exit_price=self._actual_fill_exit_price(sym, current_price),
                                pnl=reconciled.get("total_pnl", 0),
                                pnl_pct=reconciled.get("floating_pnl_pct", 0),
                                balance=provider.get_balance(),
                                exit_reason=exit_reason,
                                holding_time=holding,
                            )
                            pos["closure_notified"] = True
                        except Exception:
                            pass
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
            from bot.data import fetch_tickers_cached  # noqa: PLC0415
            symbols = [p["symbol"] for p in open_positions if "symbol" in p]
            tickers = fetch_tickers_cached(
                getattr(self.config, "exchange", "binance"), symbols,
            ) if symbols else {}
        except Exception as exc:
            self.logger.debug(f"Reconciliation ticker fetch failed: {exc}")
            return

        for pos in positions:
            sym = pos.get("symbol", "")
            if pos.get("status") not in OPEN_STATUSES:
                continue

            qty = float(pos.get("quantity", 0) or 0)
            rem = float(pos.get("remaining_qty", qty) or qty)
            if qty <= 0 or rem <= 0:
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

    @staticmethod
    def _actual_fill_exit_price(symbol: str, fallback: float) -> float:
        """Weighted-average exit price from actual exchange fills.

        ``live_pending_closures.json`` accumulates every real TP/SL sell
        fill while a position is still open.  The ticker ``last`` price
        at the moment the pipeline noticed the closure is NOT the actual
        fill price (market orders fill at order-book price).
        """
        import json as _json  # noqa: PLC0415
        try:
            with open("data/live_pending_closures.json") as f:
                pending = _json.load(f)
            rec = pending.get(symbol) if isinstance(pending, dict) else None
            if rec is None:
                return fallback
            fills = rec.get("sell_fills") or []
            if not fills:
                return fallback
            total_qty = 0.0
            total_value = 0.0
            for fill in fills:
                fq = float(fill.get("qty", 0) or 0)
                fp = float(fill.get("price", 0) or 0)
                if fq > 0 and fp > 0:
                    total_qty += fq
                    total_value += fq * fp
            return round(total_value / total_qty, 8) if total_qty > 0 else fallback
        except (FileNotFoundError, _json.JSONDecodeError, OSError, ZeroDivisionError):
            return fallback

    @paper_state_writes
    def _persist_paper_state(self, provider: Any) -> None:
        """Persist paper provider state to balance/orders JSON files.

        ALL derived accounting metrics are recomputed from the canonical
        ``MetricsManager.compute_snapshot()`` so ``paper_balance.json``
        can never drift: ``net_pnl``, ``total_return_pct``,
        ``realized_pnl``, ``unrealized_pnl``, trade counts, and win rate
        are always consistent with the current balance and open positions.
        """
        import json  # noqa: PLC0415

        from scripts.metrics_manager import MetricsManager  # noqa: PLC0415

        balance = provider.get_balance()

        realized_pnl = 0.0
        closed_positions = []
        open_positions = []
        for vp in getattr(provider, "positions", {}).values():
            status = getattr(vp, "status", "")
            realized_pnl += getattr(vp, "realized_pnl", 0.0)
            if status != "OPEN":
                closed_positions.append(vp)
            else:
                open_positions.append({
                    "current_price": getattr(vp, "current_price", 0.0),
                    "remaining_qty": getattr(vp, "remaining_qty", 0.0),
                    "unrealized_pnl": getattr(vp, "unrealized_pnl", 0.0),
                })

        total_trades = len(closed_positions)
        winning_trades = sum(
            1 for vp in closed_positions if getattr(vp, "total_pnl", 0) > 0
        )
        losing_trades = total_trades - winning_trades
        win_rate = (winning_trades / total_trades * 100.0) if total_trades else 0.0
        gross_profit = sum(
            getattr(vp, "total_pnl", 0) for vp in closed_positions
            if getattr(vp, "total_pnl", 0) > 0
        )
        gross_loss = abs(sum(
            getattr(vp, "total_pnl", 0) for vp in closed_positions
            if getattr(vp, "total_pnl", 0) <= 0
        ))
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else (0.0 if gross_profit == 0 else 0.0)
        )

        initial = self.config.account_balance
        bal_path = "data/paper_balance.json"
        try:
            with open(bal_path) as f:
                pb = json.load(f)
            initial = pb.get("initial_balance", initial)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pb = {}

        snapshot = MetricsManager.compute_snapshot(
            cash=balance,
            realized_pnl=realized_pnl,
            initial_balance=initial,
            open_positions=open_positions,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        )

        pb["final_balance"] = round(balance, 2)
        pb["final_equity"] = round(snapshot.equity, 2)
        pb["realized_pnl"] = round(realized_pnl, 2)
        pb["unrealized_pnl"] = round(snapshot.unrealized_pnl, 2)
        pb["net_pnl"] = round(snapshot.net_pnl, 2)
        pb["total_return_pct"] = round(snapshot.total_return_pct, 2)
        pb["total_trades"] = total_trades
        pb["winning_trades"] = winning_trades
        pb["losing_trades"] = losing_trades
        pb["win_rate"] = round(win_rate, 2)
        pb["profit_factor"] = round(profit_factor, 2)
        pb["gross_profit"] = round(gross_profit, 2)
        pb["gross_loss"] = round(gross_loss, 2)

        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(bal_path, pb, indent=2)

        # Keep the CLOSED-TRADES store (paper_trade_history.csv) in sync with
        # the authoritative ledger so /history, /summary and accounting never
        # diverge.
        try:
            from scripts.paper_state_lock import rebuild_trade_history_csv
            rebuild_trade_history_csv("data")
        except OSError:
            pass
