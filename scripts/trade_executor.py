"""
Professional Trade Executor for ZetBot AI

Consumes risk-manager decisions and prepares final execution plans
for every approved trade.  This module does NOT send live orders.

Usage::

    python -m scripts.trade_executor
"""

import csv
import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from scripts.risk_manager import dynamic_max_positions, _resolve_account_state
from scripts.money_management import (
    DAILY_LOSS_LIMIT as MM_DAILY_LOSS_LIMIT,
    MAX_OPEN_POSITIONS as MM_MAX_OPEN_POSITIONS,
)

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

RISK_RESULTS_PATH = "data/risk_results.json"
DECISION_RESULTS_PATH = "data/decision_results.json"
PAPER_STATE_PATH = "data/paper_state.json"

# Simulated exchange requirements (Binance spot defaults)
EXCHANGE_MIN_NOTIONAL = 10.0        # 10 USDT minimum order value
EXCHANGE_MIN_QTY_DEFAULT = 0.00001  # default min quantity step

# NOTE: max_positions and max_daily_loss are equity-relative by default
# (see ExecutionValidator / TradeExecutor below) — these constants are
# only the fallback used when no equity figure can be resolved at all.
# Sourced from scripts.money_management so this module can never drift
# from SPECIFICATION.md §25/§47/§49's production Money Management
# defaults (1 max open position, 3% daily loss limit).
MAX_OPEN_POSITIONS = MM_MAX_OPEN_POSITIONS
MAX_DAILY_LOSS_PCT = MM_DAILY_LOSS_LIMIT * 100.0   # % of current equity
MIN_RR = 1.5

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------



@dataclass
class RiskApproval:
    """An approved trade from the risk manager."""
    symbol: str
    probability: float
    position_size: float
    position_value: float
    entry_price: float
    stop_loss: float
    stop_method: str
    stop_distance_pct: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    risk_amount: float
    risk_percent: float
    reward_amount: float
    expected_rr: float
    approval: str
    rejection_reason: str
    venue: str = "cex"


@dataclass
class DecisionScores:
    """Scores from the decision engine for one symbol."""
    symbol: str
    probability: float
    recommendation: str
    risk_score: float
    reward_score: float
    trend_score: float
    momentum_score: float
    volume_score: float
    volatility_score: float
    expected_rr: float
    overall_score: float
    gate_reasons: list[str] = field(default_factory=list)


@dataclass
class TradeExecution:
    """Final execution plan for one trade.

    This is the complete specification needed by an execution layer
    (manual or automated) to place the order.
    """
    symbol: str
    entry_price: float
    position_size_usdt: float
    quantity: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_amount: float
    reward_amount: float
    risk_reward: float
    probability: float
    recommendation: str
    confidence: float
    signal_time: str
    status: str
    rejection_reason: str
    venue: str = "cex"


# ---------------------------------------------------------------------------
#  Data loader
# ---------------------------------------------------------------------------


class DataLoader:
    """Load risk and decision results from disk."""

    @staticmethod
    def load_risk_results(path: str) -> list[RiskApproval]:
        with open(path) as f:
            data = json.load(f)
        results: list[RiskApproval] = []
        for r in data.get("results", []):
            results.append(RiskApproval(
                symbol=r["symbol"],
                probability=r.get("probability", 0.0),
                position_size=r.get("position_size", 0.0),
                position_value=r.get("position_value", 0.0),
                entry_price=r.get("entry_price", 0.0),
                stop_loss=r.get("stop_loss", 0.0),
                stop_method=r.get("stop_method", ""),
                stop_distance_pct=r.get("stop_distance_pct", 0.0),
                take_profit_1=r.get("take_profit_1", 0.0),
                take_profit_2=r.get("take_profit_2", 0.0),
                take_profit_3=r.get("take_profit_3", 0.0),
                risk_amount=r.get("risk_amount", 0.0),
                risk_percent=r.get("risk_percent", 0.0),
                reward_amount=r.get("reward_amount", 0.0),
                expected_rr=r.get("expected_rr", 0.0),
                approval=r.get("approval", ""),
                rejection_reason=r.get("rejection_reason", ""),
                venue=r.get("venue", "cex"),
            ))
        return results

    @staticmethod
    def load_decisions(path: str) -> dict[str, DecisionScores]:
        with open(path) as f:
            data = json.load(f)
        result: dict[str, DecisionScores] = {}
        for d in data.get("decisions", []):
            result[d["symbol"]] = DecisionScores(
                symbol=d["symbol"],
                probability=d.get("probability", 0.0),
                recommendation=d.get("recommendation", ""),
                risk_score=d.get("risk_score", 0.0),
                reward_score=d.get("reward_score", 0.0),
                trend_score=d.get("trend_score", 0.0),
                momentum_score=d.get("momentum_score", 0.0),
                volume_score=d.get("volume_score", 0.0),
                volatility_score=d.get("volatility_score", 0.0),
                expected_rr=d.get("expected_rr", 0.0),
                overall_score=d.get("overall_score", 0.0),
                gate_reasons=list(d.get("gate_reasons") or []),
            )
        return result


# ---------------------------------------------------------------------------
#  Execution validator
# ---------------------------------------------------------------------------


def _count_open_positions() -> int:
    """Count positions genuinely open right now, carried over from
    previous pipeline cycles.

    Without this, ``ExecutionValidator._used_positions`` only ever
    reflected trades approved within the current batch — so
    ``MAX_OPEN_POSITIONS`` was silently reset to 0 every run and could
    never account for positions still open from earlier cycles,
    letting real exposure grow past the configured cap.
    """
    count = 0
    try:
        with open(PAPER_STATE_PATH) as f:
            paper_state = json.load(f)
        for vp in paper_state.get("positions", {}).values():
            if vp.get("status") == "OPEN":
                count += 1
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    from scripts.live_position_sync import count_live_open_positions  # noqa: PLC0415
    count += count_live_open_positions()

    return count


class ExecutionValidator:
    """Final validation before an execution plan is created.

    Tracks cumulative state (open positions, daily loss) across
    all trades in the plan.
    """

    def __init__(
        self,
        min_notional: float = EXCHANGE_MIN_NOTIONAL,
        min_rr: float = MIN_RR,
        equity: float = 10_000.0,
        max_positions: int | None = None,
        max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT,
        max_daily_loss: float | None = None,
    ) -> None:
        """
        Parameters
        ----------
        equity : float
            Current account equity. Base for both the daily-loss cap
            (``equity * max_daily_loss_pct / 100``) and, when
            ``max_positions`` is not explicitly given, the equity-tiered
            position count from :func:`dynamic_max_positions`.
        max_positions : int | None
            Explicit override. If ``None`` (default), falls back to
            ``MAX_OPEN_POSITIONS`` (1) per SPECIFICATION.md
            §25/§47/§49 — a fixed cap regardless of account size.
            Position *size* (not position *count*) is what scales
            with equity — see :mod:`scripts.money_management`.
        max_daily_loss : float | None
            Explicit $ override for the daily loss cap. If ``None``
            (default), computed as ``equity * max_daily_loss_pct / 100``
            instead of a fixed dollar figure — so the cap scales with
            the account instead of assuming a $10,000 baseline.
        """
        self.min_notional = min_notional
        self.min_rr = min_rr
        self.equity = equity
        self.max_positions = (
            max_positions if max_positions is not None
            else MAX_OPEN_POSITIONS
        )
        self.max_daily_loss = (
            max_daily_loss if max_daily_loss is not None
            else equity * (max_daily_loss_pct / 100.0)
        )
        self._used_positions = 0
        self._used_daily_risk = 0.0
        self._seen_symbols: set[str] = set()

    def validate(
        self,
        risk: RiskApproval,
        decision: DecisionScores | None,
    ) -> tuple[str, str]:
        """Return (status, reason)."""
        _qc = os.getenv("QUOTE_CURRENCY", "USDT").upper()
        reasons: list[str] = []

        # 1. Invalid price
        if risk.entry_price <= 0:
            reasons.append("Invalid entry price")

        # 2. Missing indicators (no decision data)
        if decision is None:
            reasons.append("Missing decision data")

        # 2b. Hard anti-pump / chase gates (decision engine). A coin the
        #     engine gated (volume dried up, just pumped, overbought,
        #     overextended) must never reach a READY plan even if its
        #     raw probability is high — this is what stopped the bot from
        #     buying ALICE/BNB right after their pumps ended.
        if decision is not None and decision.recommendation == "IGNORE":
            why = "; ".join(decision.gate_reasons) if decision.gate_reasons \
                else "gated by decision engine"
            reasons.append(f"hard gate: {why}")

        # 3. R:R too low
        if risk.expected_rr < self.min_rr:
            reasons.append(
                f"R:R {risk.expected_rr:.2f} < {self.min_rr:.1f}"
            )

        # 4. Quantity / position too small
        quantity = risk.position_value / risk.entry_price \
            if risk.entry_price > 0 else 0.0
        if risk.position_value < self.min_notional:
            reasons.append(
                f"Position {risk.position_value:.2f} {_qc} "
                f"< {self.min_notional:.0f} {_qc} min notional"
            )

        # 5. Duplicate symbol
        if risk.symbol in self._seen_symbols:
            return "SKIPPED", f"Duplicate symbol {risk.symbol}"

        # 6. Max open positions
        if self._used_positions >= self.max_positions:
            reasons.append(
                f"Max positions ({self.max_positions}) reached"
            )

        # 7. Daily loss limit
        if self._used_daily_risk + risk.risk_amount > self.max_daily_loss:
            reasons.append("Daily loss limit reached")

        if not reasons:
            return "READY", ""

        return "REJECTED", "; ".join(reasons[:2])

    def commit(self, risk: RiskApproval) -> None:
        """Record a READY trade in the tracker state."""
        self._used_positions += 1
        self._used_daily_risk += risk.risk_amount
        self._seen_symbols.add(risk.symbol)


# ---------------------------------------------------------------------------
#  Confidence calculation
# ---------------------------------------------------------------------------


def _compute_confidence(
    probability: float,
    overall_score: float | None,
) -> float:
    """Blend probability and decision overall score into a confidence
    percentage that reflects both the statistical likelihood and the
    broader quality assessment."""
    os = overall_score if overall_score is not None else probability
    raw = probability * 0.6 + os * 0.4
    return round(max(0.0, min(100.0, raw)), 1)


# ---------------------------------------------------------------------------
#  Quantity rounding
# ---------------------------------------------------------------------------


def _round_quantity(qty: float) -> float:
    """Round quantity to a reasonable precision.

    Uses a simple heuristic based on magnitude rather than trying to
    replicate exchange-specific step sizes.
    """
    if qty <= 0:
        return 0.0
    if qty >= 1000:
        return round(qty, 2)
    if qty >= 1:
        return round(qty, 4)
    if qty >= 0.001:
        return round(qty, 6)
    return round(qty, 8)


# ---------------------------------------------------------------------------
#  TradeExecutor (orchestrator)
# ---------------------------------------------------------------------------


class TradeExecutor:
    """Orchestrate the execution planning pipeline.

    Reads approved trades from the risk manager, validates each one,
    computes derived fields (quantity, confidence), and produces a
    sorted execution plan.
    """

    def __init__(self, equity: float | None = None) -> None:
        """
        Parameters
        ----------
        equity : float | None
            Current account equity, used to scale ``max_positions`` and
            the daily-loss cap. If ``None`` (default), resolved from
            the same canonical ``data/paper_balance.json`` source the
            risk manager and Telegram use, so all three always agree.
        """
        if equity is None:
            _, equity = _resolve_account_state()
        self.equity = equity
        self.validator = ExecutionValidator(equity=equity)
        self.executions: list[TradeExecution] = []

    def run(self) -> list[TradeExecution]:
        """Full execution planning pipeline."""
        _qc = os.getenv("QUOTE_CURRENCY", "USDT").upper()
        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — PROFESSIONAL TRADE EXECUTOR")
        print(f"  {'=' * 78}")
        print(f"  Equity           : {self.equity:>8,.2f} {_qc}")
        print(f"  Max positions    : {self.validator.max_positions}")
        print(f"  Max daily loss   : {self.validator.max_daily_loss:>8,.2f} {_qc}  "
              f"({MAX_DAILY_LOSS_PCT:.1f}% of equity)")
        print()

        t0 = time.time()

        # Seed with real open positions carried over from prior cycles —
        # otherwise MAX_OPEN_POSITIONS only ever counted this batch and
        # could never see exposure already on the books.
        self.validator._used_positions = _count_open_positions()

        # 1. Load data
        print("  [1/4] Loading data … ", end="", flush=True)
        all_risks = DataLoader.load_risk_results(RISK_RESULTS_PATH)
        decision_map = DataLoader.load_decisions(DECISION_RESULTS_PATH)
        print(f"{len(all_risks)} risk results, "
              f"{len(decision_map)} decision scores")

        # 2. Filter approved
        approved = [r for r in all_risks if r.approval == "APPROVED"]
        print(f"  [2/4] Approved trades  : {len(approved)}")

        # /pause must block NEW positions from being opened. It must NOT
        # be treated as cosmetic status-only — this is the actual gate.
        # Existing open positions are still reconciled/closed downstream
        # (position_manager / paper engine handle them independent of
        # trade_plan.json READY entries), so pausing only stops new entries.
        trading_paused = os.path.exists("data/.paused")
        if trading_paused:
            print("  [2/4] Trading PAUSED — no new positions will be opened.")

        # 3. Validate & build execution plans
        print("  [3/4] Validating …", flush=True)
        signal_ts = datetime.now(timezone.utc).isoformat()
        plans: list[TradeExecution] = []

        for risk in approved:
            decision = decision_map.get(risk.symbol)

            previous_signal = ""

            try:
                with open("data/trade_plan.json") as f:
                    old = json.load(f)

                for p in old.get("plans", []):
                    if (
                        p.get("symbol") == risk.symbol
                        and abs(
                            p.get("entry_price", 0.0) - risk.entry_price
                        ) < 1e-8
                    ):
                        previous_signal = p.get("signal_time", "")
                        break

            except (FileNotFoundError, json.JSONDecodeError):
                pass

            quantity = _round_quantity(
                risk.position_value / risk.entry_price
                if risk.entry_price > 0 else 0.0
            )

            confidence = _compute_confidence(
                risk.probability,
                decision.overall_score if decision else None,
            )

            status, reason = self.validator.validate(risk, decision)
            if trading_paused and status == "READY":
                status, reason = "REJECTED", "Trading paused (see /resume)"

            if status == "READY":
                self.validator.commit(risk)
                print(f"    READY   {risk.symbol:>12s}  "
                      f"conf={confidence:.1f}  "
                      f"R:R {risk.expected_rr:.2f}  "
                      f"{risk.position_value:>7,.2f} {_qc}")
            else:
                print(f"    {status:>8s} {risk.symbol:>12s}  {reason}")

            plans.append(TradeExecution(
                symbol=risk.symbol,
                entry_price=risk.entry_price,
                position_size_usdt=round(risk.position_value, 2),
                quantity=quantity,
                stop_loss=risk.stop_loss,
                tp1=risk.take_profit_1,
                tp2=risk.take_profit_2,
                tp3=risk.take_profit_3,
                risk_amount=risk.risk_amount,
                reward_amount=risk.reward_amount,
                risk_reward=risk.expected_rr,
                probability=risk.probability,
                recommendation=decision.recommendation
                if decision else "",
                confidence=confidence,
                signal_time=previous_signal or signal_ts,
                status=status,
                rejection_reason=reason,
                venue=risk.venue,
            ))

        plans.sort(key=lambda p: p.probability, reverse=True)
        self.executions = plans
        elapsed = time.time() - t0

        # 4. Report
        print(f"  [4/4] Generating report …", flush=True)
        self._print_summary(elapsed)

        return plans

    def _print_summary(self, elapsed: float) -> None:
        _qc = os.getenv("QUOTE_CURRENCY", "USDT").upper()
        ready = [p for p in self.executions if p.status == "READY"]
        rejected = [p for p in self.executions if p.status == "REJECTED"]
        skipped = [p for p in self.executions if p.status == "SKIPPED"]

        print()
        print(f"  {'=' * 78}")
        print(f"  TRADE EXECUTOR — RESULTS")
        print(f"  {'=' * 78}")
        print(f"  Total symbols  : {len(self.executions)}")
        print(f"    Approved     : {len([p for p in self.executions if p.status != 'REJECTED' or 'REJECTED' in [p.status]])}")
        # Simpler:
        approved_count = len(self.executions)
        print(f"    Ready        : {len(ready)}")
        print(f"    Rejected     : {len(rejected)}")
        print(f"    Skipped      : {len(skipped)}")
        print()

        if ready:
            avg_rr = sum(p.risk_reward for p in ready) / len(ready)
            avg_pos = sum(p.position_size_usdt for p in ready) / len(ready)
            largest = max(p.position_size_usdt for p in ready)
            smallest = min(p.position_size_usdt for p in ready)
            total_risk = sum(p.risk_amount for p in ready)
            total_reward = sum(p.reward_amount for p in ready)

            print(f"  Ready Trade Summary:")
            print(f"    Avg R:R            : {avg_rr:.2f}")
            print(f"    Avg Position       : {avg_pos:>8,.2f} {_qc}")
            print(f"    Largest Position   : {largest:>8,.2f} {_qc}")
            print(f"    Smallest Position  : {smallest:>8,.2f} {_qc}")
            print(f"    Total Risk         : {total_risk:>8,.2f} {_qc}")
            print(f"    Total Expected Rwd : {total_reward:>8,.2f} {_qc}")
            print()

        print(f"  Execution time : {elapsed:.2f}s")
        print(f"  {'=' * 78}")
        print()

        # Leaderboard
        if ready:
            print(f"  EXECUTION PLAN (sorted by confidence):")
            hdr = (
                f"  {'#':>3s} {'Pair':>12s} {'Conf%':>6s} "
                f"{'Size':>10s} {'Qty':>12s} {'Entry':>10s} "
                f"{'Stop':>10s} {'RR':>5s}"
            )
            print(hdr)
            print(f"  {'-' * (len(hdr) - 2)}")

            ready.sort(key=lambda p: p.confidence, reverse=True)
            for i, p in enumerate(ready, 1):
                qty_str = _fmt_qty(p.quantity)
                size_str = f"{p.position_size_usdt:,.0f} {_qc}"
                print(
                    f"  {i:3d} {p.symbol:>12s} {p.confidence:6.1f} "
                    f"{size_str:>10s} {qty_str:>12s} "
                    f"{p.entry_price:>10.4f} {p.stop_loss:>10.4f} "
                    f"{p.risk_reward:5.2f}"
                )
            print()


def _fmt_qty(qty: float) -> str:
    if qty >= 1_000_000:
        return f"{qty / 1_000_000:.2f}M"
    if qty >= 1_000:
        return f"{qty / 1_000:.2f}K"
    if qty >= 1:
        return f"{qty:.4f}"
    if qty >= 0.001:
        return f"{qty:.6f}"
    return f"{qty:.8f}"


# ---------------------------------------------------------------------------
#  File export
# ---------------------------------------------------------------------------


class PlanExport:
    """Write execution plan to CSV and JSON."""

    @staticmethod
    def to_csv(plans: list[TradeExecution], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "symbol", "entry_price",
            "position_size_usdt", "quantity",
            "stop_loss", "tp1", "tp2", "tp3",
            "risk_amount", "reward_amount", "risk_reward",
            "probability", "recommendation", "confidence",
            "signal_time", "status", "rejection_reason", "venue",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for p in plans:
                row = asdict(p)
                w.writerow({k: row[k] for k in fields})
        print(f"  CSV exported  : {path}")

    @staticmethod
    def to_json(plans: list[TradeExecution], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_plans": len(plans),
            "ready_count": sum(1 for p in plans if p.status == "READY"),
            "rejected_count": sum(1 for p in plans if p.status == "REJECTED"),
            "skipped_count": sum(1 for p in plans if p.status == "SKIPPED"),
            "plans": [asdict(p) for p in plans],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  JSON export   : {path}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    executor = TradeExecutor()
    plans = executor.run()

    csv_path = "data/trade_plan.csv"
    json_path = "data/trade_plan.json"

    if not plans:
        print("  No execution plans generated.  Writing empty plan file.")
        PlanExport.to_csv([], csv_path)
        PlanExport.to_json([], json_path)
        return

    PlanExport.to_csv(plans, csv_path)
    PlanExport.to_json(plans, json_path)

    print(f"\n  Completed at  : {datetime.now(timezone.utc).isoformat()}")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    main()
