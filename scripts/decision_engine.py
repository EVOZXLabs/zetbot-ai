"""
Opportunity Decision Engine for ZetBot AI

Consumes scanner results from ``data/scanner_results.json`` and computes
a final trading decision using weighted probability across six
dimensions: Trend, Momentum, Volume, Volatility, Risk, and Reward.

Usage::

    python -m scripts.decision_engine
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

SCANNER_RESULTS_PATH = "data/scanner_results.json"
TOP_N = 20

# Dimension weights (must sum to 1.0)
W_TREND = 0.20
W_MOMENTUM = 0.20
W_VOLUME = 0.15
W_VOLATILITY = 0.10
W_RISK = 0.15
W_REWARD = 0.20

# Signal thresholds
PROB_STRONG_BUY = 75.0
PROB_GOOD = 55.0
PROB_WATCHLIST = 35.0

# Hard "do-not-buy" gates (env-configurable). These reject a coin outright
# BEFORE any scoring can approve it, so the bot never chases a pump that
# already happened or a coin with no follow-through:
#   * REL_VOLUME_MIN          — relative_volume < this = volume dried up
#                               (typical right after a pump ends). Buying
#                               into dead volume = buying a local top.
#   * MAX_ATR_PCT             — reject coins that swing too violently for
#                               a sane stop (ATR% is the stop-distance base).
#   * MAX_RSI_ENTRY           — overbought: buy-the-dip only, never
#                               buy-the-top.
#   * MAX_24H_PUMP_PCT        — already pumped this much in 24h = chase risk.
#   * MAX_EMA200_EXTENSION_PCT— price stretched too far above the trend
#                               mean-reversion level.
#   * MIN_TREND_ALIGNMENT     — reject coins in a downtrend (BEARISH) and,
#                               when price sits below EMA200, structural
#                               downtrend = catching a falling knife.
REL_VOLUME_MIN = float(os.getenv("REL_VOLUME_MIN", "0.8"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "4.0"))
MAX_RSI_ENTRY = float(os.getenv("MAX_RSI_ENTRY", "65.0"))
MAX_24H_PUMP_PCT = float(os.getenv("MAX_24H_PUMP_PCT", "6.0"))
MAX_EMA200_EXTENSION_PCT = float(os.getenv("MAX_EMA200_EXTENSION_PCT", "20.0"))
MIN_TREND_ALIGNMENT = os.getenv("MIN_TREND_ALIGNMENT", "BULLISH")
MAX_PRICE_POSITION_IN_RANGE_PCT = float(os.getenv("MAX_PRICE_POSITION_IN_RANGE_PCT", "85.0"))
MAX_PRICE_VS_24H_HIGH_PCT = float(os.getenv("MAX_PRICE_VS_24H_HIGH_PCT", "97.0"))


# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------


@dataclass
class ScannerPair:
    """A single pair as read from scanner_results.json."""
    symbol: str
    base: str
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
    trend_score: float
    momentum_score: float
    volume_score: float
    volatility_score: float
    liquidity_score: float
    overall: float
    signal: str
    rank: int
    high_24h: float = 0.0
    low_24h: float = 0.0
    highest_high_20: float = 0.0
    lowest_low_20: float = 0.0


@dataclass
class Decision:
    """Complete decision for one trading pair."""
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


# ---------------------------------------------------------------------------
#  Scoring helpers
# ---------------------------------------------------------------------------


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den != 0 else default


def _cap(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _ema_distance(price: float, ema: float) -> float:
    """Distance as a percentage. Positive = above EMA."""
    return _safe_div(price - ema, ema) * 100.0


# ---------------------------------------------------------------------------
#  Dimension analyzers
# ---------------------------------------------------------------------------


class TrendAnalyzer:
    """Evaluate trend quality from EMA structure and alignment."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        # 1. EMA structure (0–35)
        if pair.ema50 > pair.ema100 > pair.ema200:
            s += 35.0
        elif pair.ema50 > pair.ema100 and pair.ema100 <= pair.ema200:
            s += 20.0
        elif pair.ema50 > pair.ema200:
            s += 10.0

        # 2. Trend alignment (0–25)
        if pair.trend_alignment == "BULLISH":
            s += 25.0
        elif pair.trend_alignment == "MIXED":
            s += 10.0

        # 3. Distance from EMA200 (0–25)
        d = _ema_distance(pair.price, pair.ema200)
        if 0 < d <= 5:
            s += 25.0
        elif 5 < d <= 15:
            s += 20.0
        elif 15 < d <= 25:
            s += 10.0
        elif d > 25:
            s += 5.0
        elif d <= 0:
            s += 5.0

        # 4. ADX trend stability (0–15)
        if pair.adx14 >= 30:
            s += 15.0
        elif pair.adx14 >= 25:
            s += 10.0
        elif pair.adx14 >= 20:
            s += 5.0

        return _cap(s)


class MomentumAnalyzer:
    """Evaluate momentum from RSI and ADX."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        # RSI position (0–45)
        rsi = pair.rsi14
        if 40 <= rsi <= 55:
            s += 45.0
        elif 30 <= rsi < 40:
            s += 35.0
        elif 55 < rsi <= 65:
            s += 25.0
        elif rsi < 30:
            s += 20.0
        else:
            s += 10.0

        # ADX trend strength (0–30)
        if pair.adx14 >= 35:
            s += 30.0
        elif pair.adx14 >= 25:
            s += 20.0
        elif pair.adx14 >= 20:
            s += 10.0

        # Change as momentum proxy (0–25)
        chg = pair.change_24h
        if -2 <= chg <= 3:
            s += 20.0
        elif -5 <= chg < -2:
            s += 25.0
        elif 3 < chg <= 8:
            s += 15.0
        elif chg < -5:
            s += 10.0
        else:
            s += 5.0

        return _cap(s)


class VolumeAnalyzer:
    """Evaluate volume quality and liquidity."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        # Relative volume (0–40)
        rv = pair.relative_volume
        if rv >= 2.0:
            s += 40.0
        elif rv >= 1.5:
            s += 30.0
        elif rv >= 1.0:
            s += 20.0
        elif rv >= 0.5:
            s += 10.0

        # Dollar volume liquidity (0–35)
        v = pair.volume_24h
        if v >= 100_000_000:
            s += 35.0
        elif v >= 10_000_000:
            s += 25.0
        elif v >= 1_000_000:
            s += 15.0
        elif v >= 100_000:
            s += 8.0

        # Volume + price change confirmation (0–25)
        if rv > 1.2 and abs(pair.change_24h) > 1:
            s += 25.0
        elif rv > 1.0:
            s += 15.0
        elif rv > 0.7:
            s += 8.0

        return _cap(s)


class VolatilityAnalyzer:
    """Evaluate volatility suitability for trading."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        atr = pair.atr_pct

        # ATR% optimality (0–50)
        if 1.0 <= atr <= 3.0:
            s += 50.0
        elif 3.0 < atr <= 5.0:
            s += 35.0
        elif 0.5 <= atr < 1.0:
            s += 30.0
        elif 5.0 < atr <= 8.0:
            s += 20.0
        elif atr > 8.0:
            s += 10.0
        else:
            s += 10.0

        # Stability (0–30)
        if atr <= 4.0:
            s += 30.0
        elif atr <= 6.0:
            s += 15.0
        else:
            s += 5.0

        # Noise score (0–20)
        if 0.5 <= atr <= 5.0:
            s += 20.0
        else:
            s += 5.0

        return _cap(s)


class RiskAnalyzer:
    """Evaluate risk.  Higher score = MORE risky."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        # Distance-to-EMA200 risk (0–40)
        d = _ema_distance(pair.price, pair.ema200)
        if d > 25:
            s += 40.0
        elif d <= 0:
            s += 35.0
        elif 15 < d <= 25:
            s += 20.0
        elif 5 < d <= 15:
            s += 10.0
        else:
            s += 5.0

        # ATR stop-distance risk (0–30)
        atr = pair.atr_pct
        if atr > 6.0:
            s += 30.0
        elif atr > 4.0:
            s += 20.0
        elif atr > 2.0:
            s += 10.0
        elif atr > 0.5:
            s += 5.0

        # Liquidity risk (0–20)
        v = pair.volume_24h
        if v < 100_000:
            s += 20.0
        elif v < 500_000:
            s += 15.0
        elif v < 1_000_000:
            s += 10.0
        elif v < 10_000_000:
            s += 5.0

        # RSI extreme risk (0–10)
        rsi = pair.rsi14
        if rsi > 80 or rsi < 20:
            s += 10.0
        elif rsi > 70 or rsi < 25:
            s += 5.0

        return _cap(s)


class RewardAnalyzer:
    """Evaluate reward potential from trend and momentum."""

    @staticmethod
    def score(pair: ScannerPair) -> float:
        s = 0.0

        # Trend-strength reward (0–30)
        if pair.ema50 > pair.ema100 > pair.ema200 and pair.trend_alignment == "BULLISH":
            s += 30.0
        elif pair.trend_alignment == "BULLISH":
            s += 20.0
        elif pair.trend_alignment == "MIXED":
            s += 8.0

        # Momentum reward (0–25)
        rsi = pair.rsi14
        if 30 <= rsi <= 50 and pair.adx14 >= 25:
            s += 25.0
        elif 30 <= rsi <= 50:
            s += 15.0
        elif 50 < rsi <= 60 and pair.adx14 >= 25:
            s += 18.0
        elif 50 < rsi <= 60:
            s += 10.0
        else:
            s += 5.0

        # Volume confirmation (0–20)
        rv = pair.relative_volume
        if rv > 1.5 and pair.volume_24h > 1_000_000:
            s += 20.0
        elif rv > 1.0:
            s += 12.0
        elif rv > 0.7:
            s += 6.0

        # Trend runway — distance from EMA200 (0–25)
        d = _ema_distance(pair.price, pair.ema200)
        if 0 < d <= 10:
            s += 25.0
        elif 10 < d <= 20:
            s += 18.0
        elif 20 < d <= 30:
            s += 10.0
        elif d > 30:
            s += 5.0
        elif d <= 0:
            s += 2.0

        return _cap(s)


# ---------------------------------------------------------------------------
#  Expected R:R
# ---------------------------------------------------------------------------


def _gate_reasons(pair: ScannerPair) -> list[str]:
    """Hard "do-not-buy" checks. Returns human-readable reasons; empty
    list means the coin passes all gates. These run BEFORE the weighted
    probability so a coin that just finished pumping can never be bought
    on a bullish-looking chart — chasing tops is how the bot racks up
    -2% losses within minutes of entry.
    """
    reasons: list[str] = []

    rv = pair.relative_volume
    if rv is not None and rv < REL_VOLUME_MIN:
        reasons.append(
            f"volume dried up (rel_volume {rv:.2f} < {REL_VOLUME_MIN:.1f})"
        )

    atr = pair.atr_pct
    if atr is not None and atr > MAX_ATR_PCT:
        reasons.append(
            f"too volatile for entry (ATR {atr:.1f}% > {MAX_ATR_PCT:.1f}%)"
        )

    rsi = pair.rsi14
    if rsi is not None and rsi > MAX_RSI_ENTRY:
        reasons.append(f"overbought (RSI {rsi:.0f} > {MAX_RSI_ENTRY:.0f})")

    chg = pair.change_24h
    if chg is not None and chg > MAX_24H_PUMP_PCT:
        reasons.append(
            f"already pumped +{chg:.1f}% in 24h (cap {MAX_24H_PUMP_PCT:.0f}%)"
        )

    d = _ema_distance(pair.price, pair.ema200)
    if d > MAX_EMA200_EXTENSION_PCT:
        reasons.append(
            f"overextended {d:.1f}% above EMA200 "
            f"(cap {MAX_EMA200_EXTENSION_PCT:.0f}%)"
        )

    trend = (pair.trend_alignment or "").upper()
    if MIN_TREND_ALIGNMENT.upper() == "BULLISH" and trend != "BULLISH":
        reasons.append(f"trend {trend} (requires {MIN_TREND_ALIGNMENT.upper()})")

    # Structural downtrend: price below the long-term mean-reversion level
    # even if the short-term EMA stack looks fine — buying here is
    # catching a falling knife (the #1 source of stop-out losses).
    if pair.ema200 and pair.ema200 > 0 and pair.price < pair.ema200:
        reasons.append("below EMA200 (structural downtrend)")

    # Don't buy at the top of the recent range.  highest_high_20 and
    # lowest_low_20 define the 20-candle trading range.  If price is in
    # the top 15% of that range, the upside is limited and a reversion
    # to the mean is likely.
    hh = pair.highest_high_20
    ll = pair.lowest_low_20
    if hh > 0 and ll > 0 and hh > ll:
        range_pos = (pair.price - ll) / (hh - ll) * 100.0
        if range_pos > MAX_PRICE_POSITION_IN_RANGE_PCT:
            reasons.append(
                f"near range top ({range_pos:.0f}% of 20-candle range "
                f"> {MAX_PRICE_POSITION_IN_RANGE_PCT:.0f}%)"
            )

    # Don't chase a coin at its 24h high.  Buying within the last few
    # percent of the daily range is the #1 cause of immediate losses.
    if pair.high_24h > 0:
        pct_of_high = (pair.price / pair.high_24h) * 100.0
        if pct_of_high > MAX_PRICE_VS_24H_HIGH_PCT:
            reasons.append(
                f"at 24h high ({pct_of_high:.1f}% of high "
                f"> {MAX_PRICE_VS_24H_HIGH_PCT:.0f}%)"
            )

    return reasons


def _compute_expected_rr(pair: ScannerPair,
                         risk_score: float,
                         reward_score: float) -> float:
    """Compute expected risk-to-reward ratio.

    Uses ATR% and trend distance to estimate realistic stop-loss and
    take-profit levels, then combines with the dimension scores.
    """
    atr_stop = max(pair.atr_pct * 1.5, 0.3)
    d = _ema_distance(pair.price, pair.ema200)

    # Reward estimate: trend gives us room to run
    if d > 0 and pair.trend_alignment == "BULLISH":
        reward = max(d * 0.5, atr_stop * 1.5)
    elif d > 0:
        reward = max(atr_stop * 1.2, 0.5)
    else:
        reward = atr_stop * 0.8

    raw_rr = _safe_div(reward, atr_stop)

    # Blend with score-based ratio
    score_rr = _safe_div(reward_score + 1, risk_score + 1)

    blended = raw_rr * 0.4 + score_rr * 0.6

    return round(blended, 2)


# ---------------------------------------------------------------------------
#  Overall probability
# ---------------------------------------------------------------------------


def _compute_probability(trend: float, momentum: float, volume: float,
                         volatility: float, risk: float,
                         reward: float) -> float:
    raw = (
        trend * W_TREND
        + momentum * W_MOMENTUM
        + volume * W_VOLUME
        + volatility * W_VOLATILITY
        + (100.0 - risk) * W_RISK
        + reward * W_REWARD
    )
    return round(_cap(raw), 1)


# ---------------------------------------------------------------------------
#  Signal classification
# ---------------------------------------------------------------------------


def _classify(probability: float) -> str:
    if probability >= PROB_STRONG_BUY:
        return "STRONG BUY"
    if probability >= PROB_GOOD:
        return "GOOD"
    if probability >= PROB_WATCHLIST:
        return "WATCHLIST"
    return "IGNORE"


# ---------------------------------------------------------------------------
#  DecisionEngine
# ---------------------------------------------------------------------------


class DecisionEngine:
    """Orchestrate the full decision pipeline."""

    def __init__(self, scanner_path: str = SCANNER_RESULTS_PATH) -> None:
        self.scanner_path = scanner_path
        self.pairs: list[ScannerPair] = []
        self.decisions: list[Decision] = []

    def load(self) -> list[ScannerPair]:
        """Load and parse scanner_results.json."""
        try:
            with open(self.scanner_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

        pairs: list[ScannerPair] = []
        for p in data.get("pairs", []):
            pairs.append(ScannerPair(
                symbol=p["symbol"],
                base=p.get("base", ""),
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
                trend_score=p.get("trend_score", 0.0),
                momentum_score=p.get("momentum_score", 0.0),
                volume_score=p.get("volume_score", 0.0),
                volatility_score=p.get("volatility_score", 0.0),
                liquidity_score=p.get("liquidity_score", 0.0),
                overall=p.get("overall", 0.0),
                signal=p.get("signal", "NEUTRAL"),
                rank=p.get("rank", 0),
                high_24h=p.get("high_24h", 0.0),
                low_24h=p.get("low_24h", 0.0),
                highest_high_20=p.get("highest_high_20", 0.0),
                lowest_low_20=p.get("lowest_low_20", 0.0),
            ))
        self.pairs = pairs
        return pairs

    def evaluate(self, pair: ScannerPair) -> Decision:
        """Compute all scores and probability for one pair."""
        ts = TrendAnalyzer.score(pair)
        ms = MomentumAnalyzer.score(pair)
        vs = VolumeAnalyzer.score(pair)
        vls = VolatilityAnalyzer.score(pair)
        rs = RiskAnalyzer.score(pair)
        rws = RewardAnalyzer.score(pair)

        prob = _compute_probability(ts, ms, vs, vls, rs, rws)
        rec = _classify(prob)
        rr = _compute_expected_rr(pair, rs, rws)

        gates = _gate_reasons(pair)
        if gates:
            # A hard gate fires (pump already happened / volume dried up /
            # overbought / overextended): the coin is NOT tradeable this
            # cycle no matter how attractive its raw score looks. Keep the
            # scores for diagnostics but force IGNORE so every downstream
            # gate (Risk, TradePlanValidator) skips it.
            prob = 0.0
            rec = "IGNORE"

        return Decision(
            symbol=pair.symbol,
            probability=prob,
            recommendation=rec,
            risk_score=round(rs, 1),
            reward_score=round(rws, 1),
            trend_score=round(ts, 1),
            momentum_score=round(ms, 1),
            volume_score=round(vs, 1),
            volatility_score=round(vls, 1),
            expected_rr=rr,
            overall_score=round(prob, 1),
            gate_reasons=gates,
        )

    def run(self) -> list[Decision]:
        """Full decision pipeline."""
        print(f"\n  {'=' * 78}")
        print(f"  ZETBOT AI — OPPORTUNITY DECISION ENGINE")
        print(f"  {'=' * 78}")
        print(f"  Scanner input : {self.scanner_path}")

        t0 = time.time()

        pairs = self.load()
        print(f"  Pairs loaded  : {len(pairs)}")

        print(f"  Evaluating …  ", end="", flush=True)
        decisions = [self.evaluate(p) for p in pairs]
        print(f"{len(decisions)} decisions computed")

        decisions.sort(key=lambda d: d.probability, reverse=True)
        self.decisions = decisions

        elapsed = time.time() - t0
        print(f"  Time          : {elapsed:.2f}s")
        print()

        return decisions


# ---------------------------------------------------------------------------
#  Report
# ---------------------------------------------------------------------------


class DecisionReport:
    """Console and file output."""

    @staticmethod
    def console(decisions: list[Decision], elapsed: float) -> None:
        """Print top-N and summary to console."""
        top = decisions[:TOP_N]

        hdr = (
            f"  {'#':>3s} {'Pair':>12s} {'Prob%':>6s} {'Risk':>5s} "
            f"{'Reward':>6s} {'R:R':>5s} {'Trend':>5s} {'Moment':>6s} "
            f"{'Vol':>5s} {'Volty':>5s} {'Rec':>12s}"
        )
        print(hdr)
        print(f"  {'-' * (len(hdr) - 2)}")

        for d in top:
            print(
                f"  {top.index(d) + 1:3d} {d.symbol:>12s} "
                f"{d.probability:6.1f} {d.risk_score:5.1f} "
                f"{d.reward_score:6.1f} {d.expected_rr:5.2f} "
                f"{d.trend_score:5.1f} {d.momentum_score:6.1f} "
                f"{d.volume_score:5.1f} {d.volatility_score:5.1f} "
                f"{d.recommendation:>12s}"
            )

        print(f"\n  ({len(decisions)} total pairs, showing top {TOP_N})")
        print(f"  {'=' * 78}")

        # Summary
        probs = [d.probability for d in decisions]
        risks = [d.risk_score for d in decisions]
        rewards = [d.reward_score for d in decisions]
        rrs = [d.expected_rr for d in decisions]

        avg_prob = sum(probs) / len(probs) if probs else 0.0
        avg_risk = sum(risks) / len(risks) if risks else 0.0
        avg_reward = sum(rewards) / len(rewards) if rewards else 0.0
        avg_rr = sum(rrs) / len(rrs) if rrs else 0.0

        print()
        print(f"  Summary:")
        print(f"    Average probability : {avg_prob:6.1f}%")
        print(f"    Highest probability : {max(probs):6.1f}%")
        print(f"    Average risk score  : {avg_risk:6.1f}")
        print(f"    Average reward score: {avg_reward:6.1f}")
        print(f"    Average R:R         : {avg_rr:6.2f}")
        print(f"    Execution time      : {elapsed:6.2f}s")
        print()

        # Distribution
        counts: dict[str, int] = {}
        for d in decisions:
            counts[d.recommendation] = counts.get(d.recommendation, 0) + 1
        print(f"  Decisions:")
        for rec in ["STRONG BUY", "GOOD", "WATCHLIST", "IGNORE"]:
            print(f"    {rec:>12s}: {counts.get(rec, 0)}")
        print()

    @staticmethod
    def to_csv(decisions: list[Decision], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fields = [
            "symbol", "probability", "recommendation",
            "risk_score", "reward_score",
            "trend_score", "momentum_score",
            "volume_score", "volatility_score",
            "expected_rr", "overall_score",
        ]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for d in decisions:
                row = asdict(d)
                w.writerow({k: row[k] for k in fields})
        print(f"  CSV exported   : {path}")

    @staticmethod
    def to_json(decisions: list[Decision], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_pairs": len(decisions),
            "decisions": [asdict(d) for d in decisions],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  JSON export    : {path}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()

    engine = DecisionEngine()
    decisions = engine.run()

    if not decisions:
        print("  No decisions generated.  Exiting.")
        return

    elapsed = time.time() - t0

    report = DecisionReport()
    report.console(decisions, elapsed)

    csv_path = "data/decision_results.csv"
    report.to_csv(decisions, csv_path)

    json_path = "data/decision_results.json"
    report.to_json(decisions, json_path)

    print(f"\n  Completed at  : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Duration      : {elapsed:.2f}s")
    print(f"  {'=' * 78}")
    print()


if __name__ == "__main__":
    main()
