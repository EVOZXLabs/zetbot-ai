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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from scripts.app_config import AppConfig
from scripts.logger import PipelineLogger


STAGE_TIMEOUT = 300  # maximum seconds per pipeline stage


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
    """Orchestrate sequential execution of trading pipeline stages."""

    def __init__(self, config: AppConfig, logger: PipelineLogger) -> None:
        self.config = config
        self.logger = logger
        self.results: list[StageResult] = []

    def run(self) -> list[StageResult]:
        """Run all stages in sequence. Returns list of ``StageResult``.

        If any stage fails, subsequent stages are skipped and the
        pipeline returns immediately.
        """
        self.logger.pipeline_start()
        self._apply_config()

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
            if not result.success:
                self.logger.pipeline_end(
                    sum(r.duration for r in self.results)
                )
                return self.results

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
    #  Stage runners (delayed imports to avoid circular dependencies)
    # ------------------------------------------------------------------

    @staticmethod
    def _run_scanner() -> None:
        from scripts import scanner
        scanner.main()

    @staticmethod
    def _run_decision() -> None:
        from scripts import decision_engine
        decision_engine.main()

    @staticmethod
    def _run_risk() -> None:
        from scripts import risk_manager
        risk_manager.main()

    @staticmethod
    def _run_trade() -> None:
        from scripts import trade_executor
        trade_executor.main()

    @staticmethod
    def _run_position() -> None:
        from scripts import position_manager
        position_manager.main()

    @staticmethod
    def _run_paper() -> None:
        from scripts import paper_trading_engine
        paper_trading_engine.main()
