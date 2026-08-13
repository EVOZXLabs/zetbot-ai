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
        3. Max open positions (from .env MAX_POSITIONS)
        4. Daily loss limit
        5. Max consecutive losses
        6. Max daily trades
        7. Volatility protection
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 3.0,
        max_consecutive_losses: int = 3,
        max_daily_trades: int = 20,
        exchange_failure_window: int = 300,
        exchange_max_failures: int = 3,
        atr_spike_multiplier: float = 3.0,
        max_open_positions: int | None = None,
        live_mode: bool = False,
    ) -> None:
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._max_daily_trades = max_daily_trades
        self._exchange_failure_window = exchange_failure_window
        self._exchange_max_failures = exchange_max_failures
        self._atr_spike_multiplier = atr_spike_multiplier
        self._live_mode = live_mode
        self._account_balance: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))
        # max_open_positions: read from .env MAX_POSITIONS if not provided.
        # SafeGuard enforces this as a hard gate so no new BUY is ever
        # submitted when the portfolio is at or above the configured ceiling.
        if max_open_positions is not None:
            self._max_open_positions = max_open_positions
        else:
            try:
                self._max_open_positions = int(os.getenv("MAX_POSITIONS", "1"))
            except (ValueError, TypeError):
                self._max_open_positions = 1
        # Positions file path — overridable in tests via set_positions_path()
        # so the check never reads the real bot's live positions.json when
        # running in an isolated test environment.
        self._positions_path: str = "data/positions.json"
        # LIVE mode counts open positions from live_positions.json — the
        # exchange-truth record — never from positions.json, which is the
        # paper/trial simulation file and is not maintained in LIVE mode.
        self._live_positions_path: str = "data/live_positions.json"
        # Symbols for which a new position is being opened right now. The
        # Position stage simulates READY plans into positions.json BEFORE the
        # paper engine executes them; without this exclusion the guard would
        # count the simulated position as already open and reject the real
        # BUY of the same symbol (the smoke-test THRESHOLD/IDR incident).
        self._planned_symbols: set[str] = set()

    def set_account_balance(self, balance: float) -> None:
        self._account_balance = balance

    def set_positions_path(self, path: str) -> None:
        """Override the positions.json path (used in tests)."""
        self._positions_path = path

    def set_live_positions_path(self, path: str) -> None:
        """Override the live_positions.json path (used in tests)."""
        self._live_positions_path = path

    def set_planned_symbols(self, symbols: set[str]) -> None:
        """Record which symbols are mid-open so the max-open-positions guard
        does not treat their own simulated positions.json entries as a
        blocking open position."""
        self._planned_symbols = set(symbols)

    @staticmethod
    def _live_balance() -> float:
        """Read live balance from paper_balance.json for daily loss calculation."""
        import json  # noqa: PLC0415
        try:
            with open("data/paper_balance.json") as f:
                pb = json.load(f)
            return float(pb.get("final_balance", 0.0))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return 0.0

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

        # 3. Max open positions (hard gate from MAX_POSITIONS env var)
        ok, reason = self._check_max_open_positions()
        if not ok:
            return False, reason

        # 4. Daily loss limit
        ok, reason = self._check_daily_loss()
        if not ok:
            return False, reason

        # 5. Max consecutive losses
        ok, reason = self._check_consecutive_losses()
        if not ok:
            return False, reason

        # 6. Max daily trades
        ok, reason = self._check_daily_trades()
        if not ok:
            return False, reason

        # 7. Volatility protection
        if symbol and atr_pct > 0 and normal_atr_pct > 0:
            ok, reason = self._check_volatility(atr_pct, normal_atr_pct)
            if not ok:
                return False, reason

        return True, ""

    # ------------------------------------------------------------------
    #  Max open positions
    # ------------------------------------------------------------------

    def _check_max_open_positions(self) -> tuple[bool, str]:
        """Reject new BUY when open positions >= MAX_POSITIONS (.env).

        Reads the authoritative position count from positions.json (PAPER)
        or live_positions.json (LIVE, exchange truth) so the check is
        always against the current live state, not a stale in-memory count.
        """
        try:
            if self._live_mode:
                open_count = self._count_live_open_positions()
                limit = self._max_open_positions
                if open_count >= limit:
                    return False, (
                        f"Max open positions reached: {open_count}/{limit} "
                        f"(set MAX_POSITIONS in .env to raise this limit)"
                    )
                return True, ""
            with open(self._positions_path) as f:
                pos_data = json.load(f)
            from scripts.position_status import is_open  # noqa: PLC0415
        except (FileNotFoundError, json.JSONDecodeError, ImportError):
            pos_data = {}
            is_open = lambda status: False  # noqa: E731

        limit = self._max_open_positions
        open_count = 0
        not_open_symbols: list[str] = []
        for p in pos_data.get("positions", []):
            if not is_open(p.get("status")):
                continue
            symbol = p.get("symbol", "")
            # A position simulated by the Position stage for a symbol that is
            # being opened RIGHT NOW is not a blocking open position.
            if symbol and symbol in self._planned_symbols:
                not_open_symbols.append(symbol)
                continue
            open_count += 1
        if not_open_symbols:
            logger.debug(
                "SafeGuard: excluding %d in-flight simulated position(s) "
                "from max-open-positions check: %s",
                len(not_open_symbols), ", ".join(sorted(not_open_symbols)),
            )
        if open_count >= limit:
            return False, (
                f"Max open positions reached: {open_count}/{limit} "
                f"(set MAX_POSITIONS in .env to raise this limit)"
            )
        return True, ""

    def _count_live_open_positions(self) -> int:
        """Count open symbols in live_positions.json (exchange truth)."""
        try:
            with open(self._live_positions_path) as f:
                live_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return 0
        if isinstance(live_data, dict):
            # {"SYMBOL": {...}, ...} — every key with a non-empty record.
            symbols = [
                s for s, p in live_data.items()
                if s and isinstance(p, dict)
            ]
        else:
            symbols = [
                p.get("symbol", "") for p in live_data.get("positions", [])
                if isinstance(p, dict)
            ]
        open_count = 0
        for s in symbols:
            if not s or s in self._planned_symbols:
                continue
            open_count += 1
        return open_count

    # ------------------------------------------------------------------
    #  Daily loss limit
    # ------------------------------------------------------------------

    def _check_daily_loss(self) -> tuple[bool, str]:
        tracking = self._load_tracking()
        today_str = date.today().isoformat()

        if tracking.get("date") != today_str:
            return True, ""

        realized_pnl = tracking.get("realized_pnl", 0.0)
        balance = self._live_balance() or self._account_balance
        max_loss = balance * (self._max_daily_loss_pct / 100.0)

        if realized_pnl < 0 and abs(realized_pnl) >= max_loss:
            qc = os.getenv("QUOTE_CURRENCY", "USDT").upper()
            return False, (
                f"Daily loss limit reached: {abs(realized_pnl):.2f} {qc} loss "
                f"(limit {max_loss:.2f} {qc}, {self._max_daily_loss_pct:.1f}%)"
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
