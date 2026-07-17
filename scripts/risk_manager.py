"""
Professional Risk Manager for ZetBot AI

Consumes scanner and decision-engine outputs, calculates trade-specific
risk parameters, validates every opportunity, and produces a final list
of APPROVED / REJECTED / WAIT trades.

Usage::

    python -m scripts.risk_manager
"""

import csv
import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

SCANNER_PATH = "data/scanner_results.json"
DECISION_PATH = "data/decision_results.json"
PAPER_STATE_PATH = "data/paper_state.json"
LIVE_POSITIONS_PATH = "data/live_positions.json"

# Account config (all overridable via module-level vars)
ACCOUNT_BALANCE = 10_000.0
MAX_RISK_PER_TRADE_PCT = 2.0       # % of account at risk per trade
MAX_DAILY_LOSS_PCT = 5.0            # % max drawdown per day
MAX_OPEN_POSITIONS = 2
MIN_RR = 1.5                        # minimum acceptable risk-reward
MAX_RR = 5.0                        # cap to avoid unrealistic targets
MIN_POSITION_SIZE_USD = 10.0        # smallest trade value
MIN_PROBABILITY = 50.0              # from decision engine
MAX_ATR_PCT = 8.0                   # reject above this volatility
MIN_VOLUME_24H = 100_000.0          # minimum daily dollar volume
MAX_POSITION_SIZE_PCT = 0.6         # max % of account equity per position ($ VALUE, not risk)
STOP_ATR_MULTIPLIER = 1.5           # ATR stop distance multiplier
STOP_FIXED_PCT = 5.0                # fallback fixed stop %

# Take-profit multipliers (relative to stop distance)
TP_MULTIPLIERS = [1.0, 2.0, 3.0]

# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class ScannerData:
    symbol: str
    price: float
    volume_24h: float
    change_24h: float
    ema50: float
    ema100: float
    ema200: float
    rsi14: float
    adx14: float
    atr_pct: float
    relative_volume: float
    trend_alignment: str


@dataclass
class DecisionData:
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


@dataclass
class RiskResult:
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


# ---------------------------------------------------------------------------
#  Stop Loss Calculation
# ---------------------------------------------------------------------------


class StopLossCalculator:
    """Calculate stop-loss prices using multiple methods.

    Automatically selects the safest (tightest) valid stop.
    All stops are for LONG positions (stop below entry).
    """

    @staticmethod
    def atr_stop(price: float, atr_pct: float,
                 multiplier: float = STOP_ATR_MULTIPLIER) -> float:
        distance = price * (atr_pct / 100.0) * multiplier
        return price - distance

    @staticmethod
    def ema_stop(price: float, ema200: float) -> float | None:
        if ema200 <= 0 or ema200 >= price:
            return None
        return ema200

    @staticmethod
    def fixed_stop(price: float, pct: float = STOP_FIXED_PCT) -> float:
        return price * (1.0 - pct / 100.0)

    @classmethod
    def safest(cls, price: float, atr_pct: float,
               ema200: float) -> tuple[float, str]:
        candidates: list[tuple[float, str]] = []

        atr = cls.atr_stop(price, atr_pct)
        candidates.append((atr, "ATR"))

        ema = cls.ema_stop(price, ema200)
        if ema is not None:
            candidates.append((ema, "EMA200"))

        fixed = cls.fixed_stop(price)
        candidates.append((fixed, "Fixed%"))

        valid = [(s, m) for s, m in candidates if s < price]
        if not valid:
            return atr, "ATR"

        # Safest = highest stop (closest to entry price)
        best_stop, best_method = max(valid, key=lambda x: x[0])

        min_stop = price * 0.5
        if best_stop < min_stop:
            return atr, "ATR"

        return best_stop, best_method


# ---------------------------------------------------------------------------
#  Position Sizing
# ---------------------------------------------------------------------------


class PositionSizer:
    """Calculate safe position sizes.

    The position VALUE (notional) is always capped so that no single
    position can exceed ``max_position_pct`` of the available balance.
    The risk_amount reflects the true dollar risk for the *capped* size.
    """

    @staticmethod
    def calculate(
        balance: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
        max_position_value: float | None = None,
    ) -> tuple[float, float, float]:
        """Return (position_size_units, risk_amount, position_value).

        Parameters
        ----------
        balance : float
            Current account equity / free balance.
        risk_pct : float
            Max % of *balance* to risk on this trade (e.g. 2.0).
        entry_price : float
            Expected entry price.
        stop_price : float
            Stop-loss price.
        max_position_value : float | None
            Hard cap on the notional position value (e.g. 60 % of equity).
            If ``None`` no cap is applied (fallback to old behaviour).

        Returns
        -------
        (position_size, risk_amount, position_value)
        """
        stop_distance = entry_price - stop_price
        if stop_distance <= 0:
            return 0.0, 0.0, 0.0

        risk_amount = balance * (risk_pct / 100.0)
        position_size = risk_amount / stop_distance
        position_value = position_size * entry_price

        # ── Cap notional position value ──────────────────────────────
        if max_position_value is not None and position_value > max_position_value:
            position_value = max_position_value
            position_size = position_value / entry_price if entry_price > 0 else 0.0
            risk_amount = position_size * stop_distance

        return position_size, risk_amount, position_value


# ---------------------------------------------------------------------------
#  Take Profit
# ---------------------------------------------------------------------------


class TakeProfitCalculator:
    """Generate multi-level take-profit prices."""

    @staticmethod
    def calculate(
        entry_price: float,
        stop_price: float,
        multipliers: list[float] | None = None,
    ) -> list[float]:
        if multipliers is None:
            multipliers = TP_MULTIPLIERS
        stop_distance = entry_price - stop_price
        if stop_distance <= 0:
            return [entry_price] * len(multipliers)
        return [entry_price + stop_distance * m for m in multipliers]


# -------------------------------------------------------------------
#  Trade Validator
# -------------------------------------------------------------------


class TradeValidator:
    """Check all constraints and return APPROVED/REJECTED/WAIT."""

    def __init__(
        self,
        min_rr: float = MIN_RR,
        max_rr: float = MAX_RR,
        min_pos_usd: float = MIN_POSITION_SIZE_USD,
        max_atr: float = MAX_ATR_PCT,
        min_vol: float = MIN_VOLUME_24H,
        min_prob: float = MIN_PROBABILITY,
    ) -> None:
        self.min_rr = min_rr
        self.max_rr = max_rr
        self.min_pos_usd = min_pos_usd
        self.max_atr = max_atr
        self.min_vol = min_vol
        self.min_prob = min_prob
        self.daily_risk_used = 0.0
        self.open_positions = 0

    def validate(
        self,
        *,
        scanner: ScannerData,
        decision: DecisionData,
        actual_rr: float,
        position_value: float,
        risk_amount: float,
        stop_distance_pct: float,
        max_daily_loss: float,
        max_positions: int,
    ) -> tuple[str, str]:
        """Return (approval, reason)."""
        reasons: list[str] = []

        # 1. Probability
        if decision.probability < self.min_prob:
            reasons.append(f"Probability {decision.probability:.1f} < {self.min_prob:.0f}")

        # 2. Trend
        if scanner.trend_alignment != "BULLISH":
            reasons.append(f"Trend {scanner.trend_alignment}")

        # 3. Volume
        if scanner.volume_24h < self.min_vol:
            reasons.append(
                f"Volume ${scanner.volume_24h:,.0f} < ${self.min_vol:,.0f}"
            )

        # 4. ATR volatility
        if scanner.atr_pct > self.max_atr:
            reasons.append(f"ATR {scanner.atr_pct:.1f}% > {self.max_atr:.0f}%")

        # 5. R:R
        if actual_rr < self.min_rr:
            reasons.append(f"R:R {actual_rr:.2f} < {self.min_rr:.1f}")
        elif actual_rr > self.max_rr:
            reasons.append(f"R:R {actual_rr:.2f} > {self.max_rr:.1f} (unrealistic)")

        # 6. Position size
        if position_value < self.min_pos_usd:
            reasons.append(
                f"Position ${position_value:,.0f} < ${self.min_pos_usd:,.0f}"
            )

        # 7. Max open positions
        if self.open_positions >= max_positions:
            reasons.append(f"Max positions ({max_positions}) reached")

        # 8. Daily loss limit
        if self.daily_risk_used + risk_amount > max_daily_loss:
            reasons.append("Daily loss limit reached")

        if not reasons:
            return "APPROVED", ""

        borderline = (
            "R:R" in reasons[-1] and actual_rr >= self.min_rr * 0.8
        )
        if borderline and len(reasons) <= 1:
            return "WAIT", reasons[0]

        return "REJECTED", "; ".join(reasons[:2])


# -------------------------------------------------------------------
#  Data Loader
# -------------------------------------------------------------------


def _count_open_positions() -> int:
    """Count positions that are genuinely open right now, carried over
    from previous pipeline cycles.

    Without this, ``TradeValidator.open_positions`` only ever reflected
    trades approved within the current batch — so ``MAX_OPEN_POSITIONS``
    was silently reset to 0 every run and could never account for
    positions still open from earlier cycles, letting real exposure
    grow past the configured cap.
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

    try:
        with open(LIVE_POSITIONS_PATH) as f:
            live_positions = json.load(f)
        if isinstance(live_positions, dict):
            count += len(live_positions)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return count


class DataLoader:
    """Load and merge scanner + decision data."""

    @staticmethod
    def load_scanner(path: str) -> dict[str, ScannerData]:
        with open(path) as f:
            data = json.load(f)
        result: dict[str, ScannerData] = {}
        for p in data.get("pairs", []):
            result[p["symbol"]] = ScannerData(
                symbol=p["symbol"],
                price=p.get("price", 0.0),
                volume_24h=p.get("volume_24h", 0.0),
                change_24h=p.get("change_24h", 0.0),
                ema50=p.get("ema50", 0.0),
                ema100=p.get("ema100", 0.0),
                ema200=p.get("ema200", 0.0),
                rsi14=p.get("rsi14", 50.0),
                adx14=p.get("adx14", 0.0),
                atr_pct=p.get("atr_pct", 0.0),
                relative_volume=p.get("relative_volume", 1.0),
                trend_alignment=p.get("trend_alignment", "MIXED"),
            )
        return result

    @staticmethod
    def load_decisions(path: str) -> list[DecisionData]:
        with open(path) as f:
            data = json.load(f)
        result: list[DecisionData] = []
        for d in data.get("decisions", []):
            result.append(DecisionData(
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
            ))
        return result


# -------------------------------------------------------------------
#  RiskManager
# -------------------------------------------------------------------


class RiskManager:
    """Orchestrate the full risk-management pipeline."""

    def __init__(
        self,
        balance: float = ACCOUNT_BALANCE,
        risk_per_trade: float = MAX_RISK_PER_TRADE_PCT,
        max_daily_loss: float = MAX_DAILY_LOSS_PCT,
        max_positions: int = MAX_OPEN_POSITIONS,
        max_position_size_pct: float = MAX_POSITION_SIZE_PCT,
    ) -> None:
        self.balance = balance
        self.risk_per_trade = risk_per_trade
        self.max_daily_loss_amt = balance * (max_daily_loss / 100.0)
        self.max_positions = max_positions
        self.max_position_size_pct = max_position_size_pct
        self.validator = TradeValidator()
        self.results: list[RiskResult] = []
        self._used_capital = 0.0

    def run(self) -> list[RiskResult]:
        """Full risk-management pipeline."""
        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — PROFESSIONAL RISK MANAGER")
        print(f"  {'=' * 78}")
        print(f"  Balance          : ${self.balance:>8,.2f}")
        print(f"  Risk/trade       : {self.risk_per_trade:>5.1f}%  "
              f"(${self.balance * self.risk_per_trade / 100:>7,.2f})")
        print(f"  Max daily loss   : "
              f"{self.max_daily_loss_amt / self.balance * 100:>5.1f}%  "
              f"(${self.max_daily_loss_amt:>7,.2f})")
        print(f"  Max positions    : {self.max_positions}")
        print(f"  Min R:R          : {MIN_RR}")
        print(f"  Min probability  : {MIN_PROBABILITY:.0f}%")
        print()

        t0 = time.time()

        # 1. Load data
        print("  [1/3] Loading data … ", end="", flush=True)
        scanner_map = DataLoader.load_scanner(SCANNER_PATH)
        decisions = DataLoader.load_decisions(DECISION_PATH)
        print(f"{len(decisions)} decisions, "
              f"{len(scanner_map)} scanner pairs")

        # 2. Evaluate each decision
        print("  [2/3] Evaluating risk …", flush=True)
        results: list[RiskResult] = []
        approved_count = 0
        base_open_positions = _count_open_positions()

        for dec in decisions:
            scanner = scanner_map.get(dec.symbol)
            if scanner is None:
                continue

            # Stop loss
            stop_price, stop_method = StopLossCalculator.safest(
                scanner.price, scanner.atr_pct, scanner.ema200,
            )
            stop_distance_pct = (
                (scanner.price - stop_price) / scanner.price * 100.0
            )

            # Available capital (account equity minus already-approved positions)
            available_capital = max(0.0, self.balance - self._used_capital)
            max_pos_value = available_capital * self.max_position_size_pct

            # Position size (capped to protect account)
            pos_size, risk_amt, pos_value = PositionSizer.calculate(
                self.balance, self.risk_per_trade,
                scanner.price, stop_price,
                max_position_value=max_pos_value,
            )

            # Take profits
            tp_prices = TakeProfitCalculator.calculate(
                scanner.price, stop_price,
            )
            tp1, tp2, tp3 = tp_prices[0], tp_prices[1], tp_prices[2]

            # Use the decision engine's blended R:R estimate
            rr_for_validation = dec.expected_rr

            # Reward amount (at TP2 — more realistic expectation)
            reward_amt = pos_size * (tp2 - scanner.price)

            # Validate
            self.validator.open_positions = base_open_positions + approved_count
            self.validator.daily_risk_used = sum(
                r.risk_amount for r in results
                if r.approval == "APPROVED"
            )

            approval, reason = self.validator.validate(
                scanner=scanner,
                decision=dec,
                actual_rr=rr_for_validation,
                position_value=pos_value,
                risk_amount=risk_amt,
                stop_distance_pct=stop_distance_pct,
                max_daily_loss=self.max_daily_loss_amt,
                max_positions=self.max_positions,
            )

            if approval == "APPROVED":
                approved_count += 1
                self._used_capital += pos_value
                print(f"    APPROVED {dec.symbol:>12s}  "
                      f"R:R {rr_for_validation:.2f}  "
                      f"${pos_value:>7,.2f}")
            else:
                print(f"    {approval:>8s} {dec.symbol:>12s}  {reason}")

            results.append(RiskResult(
                symbol=dec.symbol,
                probability=dec.probability,
                position_size=round(pos_size, 6),
                position_value=round(pos_value, 2),
                entry_price=scanner.price,
                stop_loss=round(stop_price, 8),
                stop_method=stop_method,
                stop_distance_pct=round(stop_distance_pct, 2),
                take_profit_1=round(tp1, 8),
                take_profit_2=round(tp2, 8),
                take_profit_3=round(tp3, 8),
                risk_amount=round(risk_amt, 2),
                risk_percent=round(self.risk_per_trade, 2),
                reward_amount=round(reward_amt, 2),
                expected_rr=round(rr_for_validation, 2),
                approval=approval,
                rejection_reason=reason,
            ))

        self.results = results
        elapsed = time.time() - t0

        # 3. Report
        print(f"  [3/3] Generating report …", flush=True)
        self._print_summary(elapsed)

        return results

    def _print_summary(self, elapsed: float) -> None:
        approved = [r for r in self.results if r.approval == "APPROVED"]
        rejected = [r for r in self.results if r.approval == "REJECTED"]
        waiting = [r for r in self.results if r.approval == "WAIT"]

        print()
        print(f"  {'=' * 78}")
        print(f"  RISK MANAGER — RESULTS")
        print(f"  {'=' * 78}")
        print(f"  Processed     : {len(self.results)}")
        print(f"  {'Approved':>14s}: {len(approved)}")
        print(f"  {'Rejected':>14s}: {len(rejected)}")
        print(f"  {'WAIT':>14s}: {len(waiting)}")
        print()

        if approved:
            avg_rr = sum(r.expected_rr for r in approved) / len(approved)
            avg_pos = sum(r.position_value for r in approved) / len(approved)
            largest = max(r.position_value for r in approved)
            smallest = min(r.position_value for r in approved)
            total_capital = sum(r.position_value for r in approved)
            total_risk = sum(r.risk_amount for r in approved)

            print(f"  Approved Trade Summary:")
            print(f"    Avg R:R          : {avg_rr:.2f}")
            print(f"    Avg Position     : ${avg_pos:>8,.2f}")
            print(f"    Largest Position : ${largest:>8,.2f}")
            print(f"    Smallest Position: ${smallest:>8,.2f}")
            print(f"    Total Capital    : ${total_capital:>9,.2f}")
            print(f"    Total Risk       : ${total_risk:>9,.2f}")
            print()

        print(f"  Execution time   : {elapsed:.2f}s")
        print(f"  {'=' * 78}")
        print()

        # Leaderboard of approved trades
        if approved:
            print(f"  APPROVED TRADES (sorted by probability):")
            hdr = (
                f"  {'#':>3s} {'Pair':>12s} {'Prob%':>6s} "
                f"{'Size':>10s} {'Entry':>10s} {'Stop':>10s} "
                f"{'Method':>6s} {'TP1':>10s} {'R:R':>5s}"
            )
            print(hdr)
            print(f"  {'-' * (len(hdr) - 2)}")

            approved.sort(key=lambda r: r.probability, reverse=True)
            for i, r in enumerate(approved[:10], 1):
                size_str = f"${r.position_value:,.0f}"
                print(
                    f"  {i:3d} {r.symbol:>12s} {r.probability:6.1f} "
                    f"{size_str:>10s} {r.entry_price:>10.4f} "
                    f"{r.stop_loss:>10.4f} {r.stop_method:>6s} "
                    f"{r.take_profit_1:>10.4f} {r.expected_rr:5.2f}"
                )
            if len(approved) > 10:
                print(f"  ... {len(approved) - 10} more approved trades")
            print()


# -------------------------------------------------------------------
#  File export
# -------------------------------------------------------------------


class RiskReport:
    """Write risk results to CSV and JSON."""

    @staticmethod
    def to_csv(results: list[RiskResult], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "symbol", "probability",
            "position_size", "position_value",
            "entry_price", "stop_loss", "stop_method",
            "take_profit_1", "take_profit_2", "take_profit_3",
            "risk_amount", "risk_percent",
            "reward_amount", "expected_rr",
            "approval", "rejection_reason",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in results:
                row = asdict(r)
                w.writerow({k: row[k] for k in fields})
        print(f"  CSV exported   : {path}")

    @staticmethod
    def to_json(results: list[RiskResult], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_pairs": len(results),
            "results": [asdict(r) for r in results],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  JSON export    : {path}")


# -------------------------------------------------------------------
#  Main
# -------------------------------------------------------------------


def main() -> None:
    manager = RiskManager()
    results = manager.run()

    if not results:
        print("  No results generated.  Exiting.")
        return

    csv_path = "data/risk_results.csv"
    RiskReport.to_csv(results, csv_path)

    json_path = "data/risk_results.json"
    RiskReport.to_json(results, json_path)

    print(f"\n  Completed at  : {datetime.now(timezone.utc).isoformat()}")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    main()
