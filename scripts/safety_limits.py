"""Safety limits and trading guards for ZetBot AI.

Configurable risk limits that gate new position openings:
    - Daily loss limit
    - Max consecutive losses
    - Max daily trades
    - Exchange failure cooldown
    - Volatility protection
    - Pause mode

All limits are configurable via .env (loaded through AppConfig).
"""

import json
import os
import time
import logging
from datetime import datetime, timezone, date
from typing import Any

logger = logging.getLogger("ZetBot")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------

TRACKING_PATH = "data/safety_tracking.json"
COOLDOWN_PATH = "data/exchange_cooldown.json"
VOLATILITY_PATH = "data/volatility_state.json"

# ---------------------------------------------------------------------------
#  SafeGuard — single entry point for all pre-trade checks
# ---------------------------------------------------------------------------


class SafeGuard:
    """Gate all new position openings through this.

    Checks in order:
        1. Pause file
        2. Exchange cooldown
        3. Daily loss limit
        4. Max consecutive losses
        5. Max daily trades
        6. Volatility protection
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 5.0,
        max_consecutive_losses: int = 3,
        max_daily_trades: int = 20,
        exchange_failure_window: int = 300,
        exchange_max_failures: int = 3,
        atr_spike_multiplier: float = 3.0,
    ) -> None:
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._max_daily_trades = max_daily_trades
        self._exchange_failure_window = exchange_failure_window
        self._exchange_max_failures = exchange_max_failures
        self._atr_spike_multiplier = atr_spike_multiplier
        self._account_balance: float = 10000.0

    def set_account_balance(self, balance: float) -> None:
        self._account_balance = balance

    def can_open_new_position(
        self, symbol: str = "", atr_pct: float = 0.0, normal_atr_pct: float = 0.0
    ) -> tuple[bool, str]:
        """Check ALL guards. Returns (allowed, reason)."""
        # 1. Pause check
        if os.path.exists("data/.paused"):
            return False, "Trading paused by /pause"

        # 2. Exchange cooldown
        ok, reason = self._check_exchange_cooldown()
        if not ok:
            return False, reason

        # 3. Daily loss limit
        ok, reason = self._check_daily_loss()
        if not ok:
            return False, reason

        # 4. Max consecutive losses
        ok, reason = self._check_consecutive_losses()
        if not ok:
            return False, reason

        # 5. Max daily trades
        ok, reason = self._check_daily_trades()
        if not ok:
            return False, reason

        # 6. Volatility protection
        if symbol and atr_pct > 0 and normal_atr_pct > 0:
            ok, reason = self._check_volatility(atr_pct, normal_atr_pct)
            if not ok:
                return False, reason

        return True, ""

    # ------------------------------------------------------------------
    #  Daily loss limit
    # ------------------------------------------------------------------

    def _check_daily_loss(self) -> tuple[bool, str]:
        tracking = self._load_tracking()
        today_str = date.today().isoformat()

        if tracking.get("date") != today_str:
            return True, ""

        realized_pnl = tracking.get("realized_pnl", 0.0)
        max_loss = self._account_balance * (self._max_daily_loss_pct / 100.0)

        if realized_pnl < 0 and abs(realized_pnl) >= max_loss:
            return False, (
                f"Daily loss limit reached: ${abs(realized_pnl):.2f} loss "
                f"(limit ${max_loss:.2f}, {self._max_daily_loss_pct:.1f}%)"
            )
        return True, ""

    # ------------------------------------------------------------------
    #  Max consecutive losses
    # ------------------------------------------------------------------

    def _check_consecutive_losses(self) -> tuple[bool, str]:
        tracking = self._load_tracking()
        today_str = date.today().isoformat()

        if tracking.get("date") != today_str:
            return True, ""

        consecutive = tracking.get("consecutive_losses", 0)
        if consecutive >= self._max_consecutive_losses:
            return False, (
                f"Max consecutive losses reached: {consecutive} "
                f"(limit {self._max_consecutive_losses})"
            )
        return True, ""

    # ------------------------------------------------------------------
    #  Max daily trades
    # ------------------------------------------------------------------

    def _check_daily_trades(self) -> tuple[bool, str]:
        tracking = self._load_tracking()
        today_str = date.today().isoformat()

        if tracking.get("date") != today_str:
            return True, ""

        trades = tracking.get("total_trades", 0)
        if trades >= self._max_daily_trades:
            return False, (
                f"Max daily trades reached: {trades} (limit {self._max_daily_trades})"
            )
        return True, ""

    # ------------------------------------------------------------------
    #  Exchange cooldown
    # ------------------------------------------------------------------

    def _check_exchange_cooldown(self) -> tuple[bool, str]:
        cooldown = self._load_cooldown()
        if not cooldown.get("active"):
            return True, ""

        remaining = cooldown["until"] - time.time()
        if remaining > 0:
            return False, (
                f"Exchange cooldown active — {int(remaining)}s remaining. "
                f"Reason: {cooldown.get('reason', 'unknown')}"
            )

        self._clear_cooldown()
        return True, ""

    def record_exchange_failure(self) -> None:
        """Record an exchange API failure. If too many occur within the
        window, activate cooldown."""
        cooldown = self._load_cooldown()
        failures = cooldown.get("failures", [])
        now = time.time()
        window_start = now - self._exchange_failure_window
        failures = [t for t in failures if t > window_start]
        failures.append(now)
        cooldown["failures"] = failures

        if len(failures) >= self._exchange_max_failures:
            cooldown_duration = max(300, self._exchange_failure_window * 2)
            cooldown["active"] = True
            cooldown["until"] = now + cooldown_duration
            cooldown["reason"] = (
                f"{len(failures)} exchange API failures in "
                f"{self._exchange_failure_window}s"
            )
            logger.warning(
                "Exchange cooldown activated for %ds — %s",
                cooldown_duration, cooldown["reason"],
            )

        self._save_cooldown(cooldown)

    def clear_exchange_cooldown(self) -> None:
        self._clear_cooldown()

    # ------------------------------------------------------------------
    #  Volatility protection
    # ------------------------------------------------------------------

    def _check_volatility(
        self, atr_pct: float, normal_atr_pct: float
    ) -> tuple[bool, str]:
        if normal_atr_pct <= 0:
            return True, ""

        ratio = atr_pct / normal_atr_pct
        if ratio >= self._atr_spike_multiplier:
            return False, (
                f"Volatility spike detected: ATR {atr_pct:.2f}% is "
                f"{ratio:.1f}x normal ({normal_atr_pct:.2f}%) — "
                f"emergency cooldown"
            )
        return True, ""

    # ------------------------------------------------------------------
    #  Outcome recording
    # ------------------------------------------------------------------

    def record_trade_outcome(self, pnl: float) -> None:
        """Record a completed trade outcome for limit tracking."""
        tracking = self._load_tracking()
        today_str = date.today().isoformat()

        if tracking.get("date") != today_str:
            tracking = {
                "date": today_str,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "consecutive_losses": 0,
                "realized_pnl": 0.0,
            }

        tracking["total_trades"] = tracking.get("total_trades", 0) + 1
        tracking["realized_pnl"] = tracking.get("realized_pnl", 0.0) + pnl

        if pnl >= 0:
            tracking["winning_trades"] = tracking.get("winning_trades", 0) + 1
            tracking["consecutive_losses"] = 0
        else:
            tracking["losing_trades"] = tracking.get("losing_trades", 0) + 1
            tracking["consecutive_losses"] = (
                tracking.get("consecutive_losses", 0) + 1
            )

        self._save_tracking(tracking)

    # ------------------------------------------------------------------
    #  Persistence helpers
    # ------------------------------------------------------------------

    def _load_tracking(self) -> dict[str, Any]:
        try:
            with open(TRACKING_PATH) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_tracking(self, data: dict[str, Any]) -> None:
        os.makedirs("data", exist_ok=True)
        with open(TRACKING_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def _load_cooldown(self) -> dict[str, Any]:
        try:
            with open(COOLDOWN_PATH) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cooldown(self, data: dict[str, Any]) -> None:
        os.makedirs("data", exist_ok=True)
        with open(COOLDOWN_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def _clear_cooldown(self) -> None:
        try:
            if os.path.exists(COOLDOWN_PATH):
                os.remove(COOLDOWN_PATH)
        except OSError:
            pass
