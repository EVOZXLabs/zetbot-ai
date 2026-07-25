"""
Paper Trading Engine

Orchestrates the complete paper trading lifecycle:
fetch → analyze → evaluate → open/monitor/close → statistics.

ZetBot AI
"""

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bot.config import CONFIG
from bot.data import MarketData
from bot.indicators import IndicatorEngine
from bot.paper import PaperTrader
from bot.state import STATE_VERSION, StateManager
from bot.strategy import BUY, SELL, StrategyEngine
from bot.telegram import TelegramNotifier

logger = logging.getLogger("ZetBot")

# ---------------------------------------------------------------------------
#  State constants
# ---------------------------------------------------------------------------

IDLE = "IDLE"
ANALYZE = "ANALYZE"
EVALUATE = "EVALUATE"
BUY_SIGNAL = "BUY"
MONITOR = "MONITOR"
SELL_SIGNAL = "SELL"

# ---------------------------------------------------------------------------
#  Engine
# ---------------------------------------------------------------------------


class PaperTradingEngine:
    """Complete paper trading engine that ties indicators, strategy,
    position management, and statistics into a single callable cycle.

    Call :meth:`run_once` to execute one full iteration:

        IDLE → ANALYZE → EVALUATE → BUY / MONITOR / SELL → IDLE

    The engine never touches a live exchange for order execution.
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
    ) -> None:
        self._state: str = IDLE
        self._market_data: MarketData = MarketData(
            exchange_name=str(CONFIG.get("exchange", "binance")),
        )
        self._strategy: StrategyEngine = StrategyEngine(
            adx_threshold=float(CONFIG.get("adx_threshold", 25)),
            atr_multiplier=float(CONFIG.get("atr_multiplier", 0.5)),
            volatility_period=int(CONFIG.get("volatility_lookback", 14)),
            compression_lookback=int(CONFIG.get("price_compression_lookback", 20)),
            compression_ratio=float(CONFIG.get("compression_ratio", 0.3)),
        )
        self._paper: PaperTrader = PaperTrader(initial_balance=initial_balance)

        self._last_signal: dict[str, Any] | None = None
        self._last_market_state: str | None = None
        self._last_price: float | None = None
        self._last_trade: dict[str, Any] | None = None
        self._state_manager: StateManager = StateManager()
        self._auto_save: bool = bool(CONFIG.get("auto_save", True))

        self._notifier: TelegramNotifier = TelegramNotifier()
        self._notified_buy_entry: datetime | None = None

        logger.info("PaperTradingEngine initialised (balance=%.2f)", initial_balance)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def run_once(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        df: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        """Execute one complete paper trading cycle.

        Args:
            symbol: Trading pair.  Defaults to config value.
            timeframe: Candle timeframe.  Defaults to config value.
            df: Optional pre-fetched DataFrame.  When ``None`` the engine
                fetches live data from the configured exchange.

        Returns:
            A dict with keys:

            - ``"state"``: final engine state (always ``"IDLE"``).
            - ``"signal"``: the strategy result dict, or ``None``.
            - ``"trade"``: closed-trade dict if a position was closed,
              otherwise ``None``.
            - ``"position"``: current open position dict, or ``None``.
            - ``"price"``: latest close price.
            - ``"market_state"``: detected market state.
        """
        symbol = symbol or str(CONFIG.get("symbol", "BTC/USDT"))
        timeframe = timeframe or str(CONFIG.get("timeframe", "1h"))

        self._state = ANALYZE
        logger.info("Paper cycle starting — %s %s", symbol, timeframe)

        # ── 1. Fetch / accept market data ─────────────────────────────
        if df is None:
            df = self._market_data.fetch_ohlcv(
                symbol=symbol, timeframe=timeframe, limit=250,
            )

        price = float(df["close"].iloc[-1])
        self._last_price = price
        market_state = self._market_data.market_state(df)
        self._last_market_state = market_state
        logger.info("Price=%.2f Market=%s", price, market_state)

        # ── 2. Evaluate ───────────────────────────────────────────────
        self._state = EVALUATE
        self._last_signal = None
        trade: dict[str, Any] | None = None
        position = self._paper.current_position()

        if position:
            self._state = MONITOR
            self._last_signal = self._strategy.evaluate(
                df, has_position=True,
            )
            trade = self._handle_open_position(price, self._last_signal)
            if trade is not None:
                self._state = SELL_SIGNAL
                self._last_trade = trade
                self._last_trade["market_state"] = market_state
                self._notified_buy_entry = None
                self._notifier.trade_closed(
                    exit_price=trade["exit_price"],
                    pnl_usd=trade["net_pnl"],
                    pnl_pct=trade["pnl_pct"],
                    balance=self._paper._balance,
                    exit_reason=trade["exit_reason"],
                    holding_time=trade["holding_time"],
                    symbol=symbol,
                    entry_price=trade.get("entry_price", 0.0),
                )
                logger.info(
                    "Trade closed — %s PnL=%+.2f (%+.2f%%)",
                    trade["exit_reason"], trade["net_pnl"], trade["pnl_pct"],
                )
        else:
            result = self._strategy.evaluate(df, has_position=False)
            self._last_signal = result
            if result["signal"] == BUY:
                self._state = BUY_SIGNAL

                atr_pct: float | None = None
                try:
                    atr_period = int(CONFIG.get("atr_period", 14))
                    atr_value = IndicatorEngine.atr(df, period=atr_period)
                    if price > 0:
                        atr_pct = (atr_value / price) * 100.0
                except (ValueError, KeyError) as exc:
                    logger.warning(
                        "ATR unavailable for %s, falling back to fixed "
                        "SL/TP %% — %s", symbol, exc,
                    )

                pos = self._paper.open_position(
                    entry_price=price,
                    symbol=symbol,
                    timeframe=timeframe,
                    reasons=result["reason"],
                    atr_pct=atr_pct,
                )
                if pos is not None and pos["entry_time"] != self._notified_buy_entry:
                    self._notified_buy_entry = pos["entry_time"]
                    size = pos["balance_before"] * (pos["position_size_percent"] / 100.0)
                    self._notifier.buy_opened(
                        symbol=pos["symbol"],
                        timeframe=pos["timeframe"],
                        exchange=str(CONFIG.get("exchange", "binance")),
                        entry_price=pos["entry_price"],
                        quantity=pos["quantity"],
                        position_size=size,
                        stop_loss=pos["stop_loss_price"],
                        take_profit=pos["take_profit_price"],
                        reasons=result["reason"],
                    )
                logger.info(
                    "BUY signal executed — entry=%.2f reasons=%s",
                    price, " | ".join(result["reason"]),
                )
            else:
                logger.info(
                    "No signal — %s reasons=%s",
                    result["signal"], " | ".join(result["reason"]),
                )

        # ── 3. Return to IDLE ─────────────────────────────────────────
        self._state = IDLE
        self._save_auto_state()

        return {
            "state": self._state,
            "signal": self._last_signal,
            "trade": trade,
            "position": self._paper.current_position(),
            "price": price,
            "market_state": market_state,
        }

    # ------------------------------------------------------------------
    #  Queries
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return a snapshot of the engine's current state.

        Returns:
            Dict with keys: ``"state"``, ``"balance"``, ``"position"``,
            ``"last_signal"``, ``"last_price"``, ``"market_state"``.
        """
        return {
            "state": self._state,
            "balance": self._paper._balance,
            "position": self._paper.current_position(),
            "last_signal": self._last_signal,
            "last_price": self._last_price,
            "market_state": self._last_market_state,
        }

    def current_balance(self) -> float:
        """Return the current virtual balance."""
        return self._paper._balance

    def current_position(self) -> dict[str, Any] | None:
        """Return the current open position, or ``None``."""
        return self._paper.current_position()

    def trade_history(self) -> list[dict[str, Any]]:
        """Return all completed trades (oldest first)."""
        return self._paper.trade_history()

    # ------------------------------------------------------------------
    #  Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> dict[str, Any]:
        """Compute trading statistics from completed trades.

        Returns:
            Dict with all statistics defined in the specification.
        """
        trades = self._paper.trade_history()
        total = len(trades)

        if total == 0:
            return {
                "total_trades": 0,
                "total_profit": 0.0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "loss_rate": 0.0,
                "profit_factor": 0.0,
                "average_win": 0.0,
                "average_loss": 0.0,
                "largest_win": 0.0,
                "largest_loss": 0.0,
                "longest_win_streak": 0,
                "longest_loss_streak": 0,
                "average_holding_time": "00:00:00",
            }

        total_profit = sum(t["net_pnl"] for t in trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total) * 100.0 if total else 0.0
        loss_rate = (loss_count / total) * 100.0 if total else 0.0

        total_win = sum(t["net_pnl"] for t in wins)
        total_loss = sum(t["net_pnl"] for t in losses)
        profit_factor = abs(total_win / total_loss) if total_loss != 0 else (
            float("inf") if total_win > 0 else 0.0
        )
        average_win = total_win / win_count if win_count else 0.0
        average_loss = total_loss / loss_count if loss_count else 0.0

        largest_win = max(t["net_pnl"] for t in wins) if wins else 0.0
        largest_loss = min(t["net_pnl"] for t in losses) if losses else 0.0

        longest_win_streak = 0
        longest_loss_streak = 0
        current_streak = 0
        current_type: str | None = None
        for t in trades:
            if t["net_pnl"] > 0:
                if current_type == "win":
                    current_streak += 1
                else:
                    current_streak = 1
                    current_type = "win"
                longest_win_streak = max(longest_win_streak, current_streak)
            else:
                if current_type == "loss":
                    current_streak += 1
                else:
                    current_streak = 1
                    current_type = "loss"
                longest_loss_streak = max(longest_loss_streak, current_streak)

        durations = [t["holding_time"] for t in trades if "holding_time" in t]
        if durations:
            avg_seconds = sum(td.total_seconds() for td in durations) / len(durations)
            avg_holding = _format_seconds(avg_seconds)
        else:
            avg_holding = "00:00:00"

        return {
            "total_trades": total,
            "total_profit": total_profit,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "profit_factor": profit_factor,
            "average_win": average_win,
            "average_loss": average_loss,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
            "longest_win_streak": longest_win_streak,
            "longest_loss_streak": longest_loss_streak,
            "average_holding_time": avg_holding,
        }

    # ------------------------------------------------------------------
    #  State persistence
    # ------------------------------------------------------------------

    def restore_state(self) -> bool:
        """Restore engine state from the last saved state file.

        If a saved state exists and is valid, the engine's paper trader
        (balance, position, trade history) is restored.

        Returns:
            ``True`` if state was successfully restored, ``False`` if
            no saved state exists or restoration failed.
        """
        if not self._state_manager.state_exists():
            logger.info("No saved state — starting fresh")
            return False

        data = self._state_manager.load()
        if data is None:
            logger.info("State load returned None — starting fresh")
            return False

        paper_state = data.get("paper", {})
        self._paper.set_state(
            balance=float(paper_state.get("balance", self._paper._balance)),
            position=paper_state.get("position"),
            trades=list(paper_state.get("trades", [])),
        )
        self._notifier.state_restored(
            balance=self._paper._balance,
            has_position=self._paper._position is not None,
            trades=len(self._paper._trades),
        )
        logger.info(
            "Engine state restored — balance=%.2f position=%s trades=%d",
            self._paper._balance,
            "YES" if self._paper._position else "NO",
            len(self._paper._trades),
        )
        return True

    def _build_save_state(self) -> dict[str, Any]:
        """Build a serialisable dict of the current engine state.

        Returns:
            Dict suitable for ``StateManager.save()``.
        """
        paper_state = self._paper.get_state()
        stats = self.statistics()
        return {
            "state_version": STATE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "exchange": str(CONFIG.get("exchange", "binance")),
            "symbol": str(CONFIG.get("symbol", "BTC/USDT")),
            "timeframe": str(CONFIG.get("timeframe", "1h")),
            "paper": paper_state,
            "statistics": stats,
        }

    def _save_auto_state(self) -> None:
        """Automatically save state if ``auto_save`` is enabled."""
        if not self._auto_save:
            return
        try:
            self._state_manager.save(self._build_save_state())
        except Exception:
            logger.exception("Auto-save failed")

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    def _handle_open_position(
        self,
        price: float,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check TP / SL / strategy exit for an open position.

        Priority: Take Profit → Stop Loss → Strategy Exit.

        Args:
            price: Current market price.
            result: Strategy evaluation result.

        Returns:
            Closed-trade dict, or ``None`` if no exit triggered.
        """
        pos = self._paper.current_position()
        if pos is None:
            return None

        if price >= pos["take_profit_price"]:
            logger.info("Take Profit hit — price=%.2f TP=%.2f", price, pos["take_profit_price"])
            return self._paper.close_position(price, "Take Profit")

        if price <= pos["stop_loss_price"]:
            logger.info("Stop Loss hit — price=%.2f SL=%.2f", price, pos["stop_loss_price"])
            return self._paper.close_position(price, "Stop Loss")

        if result is not None and result.get("signal") == SELL:
            reasons = " | ".join(result.get("reason", []))
            logger.info("Strategy Exit — %s", reasons)
            return self._paper.close_position(price, "Strategy Exit")

        return None


def _format_seconds(total_seconds: float) -> str:
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
