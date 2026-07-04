"""
Market State Detector Module & Persistent State Manager

ZetBot AI
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from bot.config import CONFIG
from bot.indicators import IndicatorEngine

logger = logging.getLogger("ZetBot")

TRENDING = "TRENDING"
SIDEWAYS = "SIDEWAYS"


class MarketStateDetector:
    """Detect whether the market is trending or sideways.

    Combines ADX trend strength, ATR volatility compression, and
    price range compression to classify market state.

    All thresholds are configurable.
    """

    def __init__(
        self,
        adx_threshold: float = 25.0,
        atr_multiplier: float = 0.5,
        volatility_period: int = 14,
        compression_lookback: int = 20,
        compression_ratio: float = 0.3,
    ) -> None:
        """Initialise the detector.

        Args:
            adx_threshold: ADX >= this value is considered trending.
            atr_multiplier: Ratio of short/long ATR below this
                signals compression.
            volatility_period: Period for short-term ATR.
            compression_lookback: Candles for short-term price range.
            compression_ratio: Ratio of short/long price range below
                this signals compression.
        """
        self.adx_threshold = adx_threshold
        self.atr_multiplier = atr_multiplier
        self.volatility_period = volatility_period
        self.compression_lookback = compression_lookback
        self.compression_ratio = compression_ratio

    def _check_adx(self, df: pd.DataFrame) -> Optional[str]:
        """Check ADX trend strength.

        Args:
            df: OHLC DataFrame.

        Returns:
            ``"TRENDING"`` if ADX >= threshold, else ``None``.
        """
        adx_val = IndicatorEngine.adx(df, period=14)
        if adx_val >= self.adx_threshold:
            logger.info(
                "Market state -> TRENDING (ADX=%.2f >= %.1f)",
                adx_val, self.adx_threshold,
            )
            return TRENDING
        return None

    def _check_atr_compression(self, df: pd.DataFrame) -> bool:
        """Check if ATR shows volatility compression.

        Compares short-term ATR to a longer-term ATR (3x period).

        Args:
            df: OHLC DataFrame.

        Returns:
            ``True`` if compressed (short ATR / long ATR < multiplier).
        """
        long_period = self.volatility_period * 3
        min_rows = long_period + 1
        if len(df) < min_rows:
            return False

        atr_short = IndicatorEngine._atr_series(df, self.volatility_period)
        atr_long = IndicatorEngine._atr_series(df, long_period)

        current_short = float(atr_short.iloc[-1])
        current_long = float(atr_long.iloc[-1])

        if current_long == 0.0:
            return False

        ratio = current_short / current_long
        is_compressed = ratio < self.atr_multiplier

        if is_compressed:
            logger.info(
                "ATR compression: short=%.2f long=%.2f ratio=%.4f",
                current_short, current_long, ratio,
            )
        return is_compressed

    def _check_price_compression(self, df: pd.DataFrame) -> bool:
        """Check price range compression.

        Compares the high-low range over the short lookback to the
        range over a longer lookback (3x).

        Args:
            df: OHLC DataFrame.

        Returns:
            ``True`` if compressed (short range / long range < ratio).
        """
        long_lookback = self.compression_lookback * 3
        if len(df) < long_lookback:
            return False

        short_range = (
            df["high"].iloc[-self.compression_lookback:].max()
            - df["low"].iloc[-self.compression_lookback:].min()
        )
        long_range = (
            df["high"].iloc[-long_lookback:].max()
            - df["low"].iloc[-long_lookback:].min()
        )

        if long_range == 0.0:
            return False

        ratio = short_range / long_range
        is_compressed = ratio < self.compression_ratio

        if is_compressed:
            logger.info(
                "Price compression: short_range=%.2f "
                "long_range=%.2f ratio=%.4f",
                short_range, long_range, ratio,
            )
        return is_compressed

    def detect(self, df: pd.DataFrame) -> str:
        """Classify market state as trending or sideways.

        Decision logic:

        1. If ADX >= ``adx_threshold`` → **TRENDING**.
        2. If ATR shows volatility compression → **SIDEWAYS**.
        3. If price range is compressed → **SIDEWAYS**.
        4. Otherwise → **SIDEWAYS** (conservative — avoid weak
           trends unless ADX confirms strength).

        Args:
            df: DataFrame with ``high``, ``low``, ``close`` columns.

        Returns:
            ``"TRENDING"`` or ``"SIDEWAYS"``.
        """
        if df is None or df.empty:
            msg = "DataFrame is empty, cannot detect market state"
            raise ValueError(msg)

        missing = [c for c in ("high", "low", "close") if c not in df.columns]
        if missing:
            msg = (
                f"Missing required column(s) {missing} for "
                f"market state detection. Available: {list(df.columns)}"
            )
            raise ValueError(msg)

        # 1. Strong trend via ADX
        adx_result = self._check_adx(df)
        if adx_result is not None:
            return adx_result

        # 2. Volatility compression
        if self._check_atr_compression(df):
            logger.info("Market state -> SIDEWAYS (ATR compression)")
            return SIDEWAYS

        # 3. Price compression
        if self._check_price_compression(df):
            logger.info("Market state -> SIDEWAYS (price compression)")
            return SIDEWAYS

        # 4. Conservative default
        logger.info("Market state -> SIDEWAYS (default, no strong trend)")
        return SIDEWAYS


# ---------------------------------------------------------------------------
#  State Manager – persistent paper-trading state
# ---------------------------------------------------------------------------

STATE_VERSION = 1


def _json_serialize(obj: Any) -> str:
    """JSON serializer that handles ``datetime`` and ``timedelta``."""
    from datetime import datetime, timedelta

    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return int(obj.total_seconds())
    msg = f"Object of type {type(obj)} is not JSON serializable"
    raise TypeError(msg)


def _parse_state(data: dict) -> dict:
    """Convert ISO datetime strings back to ``datetime`` objects.

    Mutates the dict in-place.
    """
    from datetime import timedelta

    def _fix(obj: Any) -> Any:
        if isinstance(obj, str):
            try:
                return datetime.fromisoformat(obj)
            except (ValueError, TypeError):
                return obj
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            return obj
        return obj

    # Position is nested under paper.position
    paper = data.get("paper", {})
    pos = paper.get("position")
    if pos:
        for key in ("entry_time",):
            if key in pos and isinstance(pos[key], str):
                pos[key] = _fix(pos[key])

    # Trades are nested under paper.trades
    trades = paper.get("trades", [])
    for t in trades:
        for key in ("entry_time", "exit_time"):
            if key in t and isinstance(t[key], str):
                t[key] = _fix(t[key])
        if "holding_time" in t and isinstance(t["holding_time"], (int, float)):
            t["holding_time"] = timedelta(seconds=int(t["holding_time"]))
    return data


class StateManager:
    """Persistent state management for paper trading.

    Saves and restores the full paper trading engine state to/from a
    JSON file, enabling safe recovery after restart.

    Usage::

        mgr = StateManager()
        mgr.save(engine_state_dict)
        loaded = mgr.load()
    """

    def __init__(
        self,
        state_path: str | None = None,
        backup_corrupted: bool | None = None,
    ) -> None:
        self._state_path: str = (
            state_path
            if state_path is not None
            else str(CONFIG.get("state_path", "data/state.json"))
        )
        self._backup_corrupted: bool = (
            backup_corrupted
            if backup_corrupted is not None
            else bool(CONFIG.get("backup_corrupted_state", True))
        )

        # Ensure directory exists
        os.makedirs(os.path.dirname(self._state_path), exist_ok=True)

        logger.info("StateManager initialised — path=%s", self._state_path)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def state_exists(self) -> bool:
        """Check if a saved state file exists."""
        return os.path.isfile(self._state_path)

    def save(self, state: dict) -> None:
        """Atomically write *state* to the JSON state file.

        Args:
            state: A dict containing the full engine state.
        """
        tmp_path = self._state_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f, default=_json_serialize, indent=2)
            os.replace(tmp_path, self._state_path)
            logger.info("State saved to %s", self._state_path)
        except Exception:
            # Clean up temp file on failure
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
            raise

    def load(self) -> dict | None:
        """Load saved state from disk.

        Returns:
            Parsed state dict, or ``None`` if the file does not exist or
            is corrupted (a backup is created if configured).
        """
        if not self.state_exists():
            logger.info("No saved state found at %s", self._state_path)
            return None

        try:
            with open(self._state_path) as f:
                raw = json.load(f)

            validated = self._validate(raw)
            if validated is None:
                return None

            return _parse_state(validated)

        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("Failed to load state: %s", exc)
            self._handle_corrupted()
            return None

    def clear(self) -> None:
        """Delete the saved state file, if it exists."""
        if self.state_exists():
            os.remove(self._state_path)
            logger.info("State file removed — %s", self._state_path)

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    def _validate(self, raw: Any) -> dict | None:
        """Validate the loaded state dict.

        Args:
            raw: The deserialized JSON content.

        Returns:
            The validated dict, or ``None`` if invalid.
        """
        if not isinstance(raw, dict):
            logger.warning("Corrupted state: expected dict, got %s", type(raw).__name__)
            self._handle_corrupted()
            return None

        if "state_version" not in raw or not isinstance(raw["state_version"], int):
            logger.warning("Corrupted state: missing or invalid state_version")
            self._handle_corrupted()
            return None

        # Support both top-level (legacy) and nested paper.* (current) layouts
        paper = raw.get("paper", {})
        balance = raw.get("balance", paper.get("balance"))
        trades = raw.get("trades", paper.get("trades"))

        if balance is None:
            logger.warning("Corrupted state: missing balance")
            self._handle_corrupted()
            return None

        if trades is None or not isinstance(trades, list):
            logger.warning("Corrupted state: missing or invalid trades")
            self._handle_corrupted()
            return None

        return raw

    def _handle_corrupted(self) -> None:
        """Backup the corrupted file and log a warning."""
        if not self._backup_corrupted:
            logger.warning("Removing corrupted state file (backup disabled)")
            self.clear()
            return

        backup_path = self._state_path + ".corrupted"
        try:
            os.replace(self._state_path, backup_path)
            logger.warning(
                "Corrupted state backed up to %s — creating clean state",
                backup_path,
            )
        except OSError as exc:
            logger.error("Failed to backup corrupted state: %s", exc)
