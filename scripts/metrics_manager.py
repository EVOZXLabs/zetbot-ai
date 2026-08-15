"""Single source of truth for all bot statistics and account state.

Reads from canonical JSON files written by the pipeline and engine so
that every command sees the same values.  Provides a unified
``AccountSnapshot`` dataclass and computed metrics (streaks, expectancy,
daily/weekly/monthly aggregates).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from scripts.balance_resolver import resolve_initial_balance
from scripts.position_status import is_open

logger = logging.getLogger(__name__)


_WIB_TZ = timezone(timedelta(hours=7))


@dataclass
class AccountSnapshot:
    """Immutable point-in-time view of the account / portfolio.

    All values are computed from canonical JSON files and open positions.
    Every Telegram command MUST read from this — never compute independently.

    Invariants:
        equity == balance + position_value
        net_pnl == realized_pnl + unrealized_pnl
        exposure_pct == (position_value / equity * 100) if equity > 0 else 0
    """
    balance: float = 0.0
    equity: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    net_pnl: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    open_positions: int = 0
    initial_balance: float = 0.0
    position_value: float = 0.0
    exposure_pct: float = 0.0


@dataclass
class ComputedMetrics:
    """Extended metrics derived from trade history."""
    avg_holding_hours: float = 0.0
    avg_roi: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    expectancy: float = 0.0
    win_streak: int = 0
    loss_streak: int = 0
    best_trade: dict[str, Any] = field(default_factory=dict)
    worst_trade: dict[str, Any] = field(default_factory=dict)


class MetricsManager:
    """Central source of truth for all account & performance data.

    All data is read from the canonical JSON files.  No in-memory
    mutable state is kept, so every call reflects the latest persisted
    snapshot.
    """

    def __init__(
        self,
        data_dir: str = "data",
        wallet: Any = None,
        mode_provider: Any = None,
        account_quote: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        # ``wallet`` and ``mode_provider`` are optional so every existing
        # caller (tests, scripts) that only passes ``data_dir`` keeps
        # working exactly as before — this class defaults to the old
        # PAPER-only behavior when they're not supplied.
        #
        # ``wallet`` — an IWalletManager-like object (see
        # ``service_container._LiveWalletAdapter``) used to fetch the
        # REAL cash balance from the exchange when the bot is running
        # LIVE. Without this, every command that goes through
        # ``account()`` kept reporting the stale paper-mode balance
        # even after the bot switched to live trading.
        # ``mode_provider`` — zero-arg callable returning "PAPER" or
        # "LIVE" (e.g. ``lambda: order_manager.mode``).
        # ``account_quote`` — the account's quote currency (e.g. "IDR").
        # When set, every open-position / closed-trade view drops symbols
        # whose quote currency differs (legacy positions from a previous
        # USDT exchange on an IDR account) and logs a [LEGACY-DATA] warning
        # instead of corrupting the accounting.
        self._wallet = wallet
        self._mode_provider = mode_provider
        self._account_quote = (
            account_quote.upper() if account_quote else None
        )

    # ------------------------------------------------------------------
    #  Mode helpers
    # ------------------------------------------------------------------

    def _is_live(self) -> bool:
        if self._mode_provider is None:
            return False
        try:
            return self._mode_provider() == "LIVE"
        except Exception:
            return False

    # ------------------------------------------------------------------
    #  File readers
    # ------------------------------------------------------------------

    @staticmethod
    def _symbol_quote(symbol: str) -> str:
        if not symbol or "/" not in symbol:
            return ""
        return symbol.split("/", 1)[1].upper()

    def _passes_quote_filter(self, symbol: str) -> bool:
        """True when *symbol* is allowed on this account.

        When ``account_quote`` is configured (e.g. "IDR"), a symbol quoted
        in a different currency (e.g. ``BTC/USDT`` on an IDR account) is a
        legacy artifact from a previous exchange and must NOT pollute the
        accounting.  Returns True when no filter is configured so legacy
        single-exchange setups are unaffected.
        """
        if not self._account_quote:
            return True
        return self._symbol_quote(symbol) == self._account_quote

    def _read_json(self, filename: str) -> dict[str, Any]:
        path = os.path.join(self._data_dir, filename)
        try:
            with open(path) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _read_orders(self) -> list[dict[str, Any]]:
        d = self._read_json("paper_orders.json")
        return d if isinstance(d, list) else d.get("orders", [])

    def _read_positions(self) -> list[dict[str, Any]]:
        if self._is_live():
            return self._read_live_positions_normalized()
        d = self._read_json("positions.json")
        return d if isinstance(d, list) else d.get("positions", [])

    def _read_live_positions_normalized(self) -> list[dict[str, Any]]:
        """Adapt ``live_positions.json`` (exchange-sync schema: symbol,
        quantity, entry_price, current_price, pnl_pct — written by
        ``scripts.live_position_sync``) to the shape ``compute_snapshot()``
        expects (``remaining_qty``, ``unrealized_pnl``, ``status``), so
        LIVE and PAPER share the exact same canonical accounting path
        instead of LIVE silently reporting zero exposure/PnL.
        """
        live = self._read_json("live_positions.json")
        raw = list(live.values()) if isinstance(live, dict) else []
        normalized: list[dict[str, Any]] = []
        for p in raw:
            qty = p.get("quantity", 0.0) or 0.0
            entry = p.get("entry_price")
            current = p.get("current_price")
            unrealized = (current - entry) * qty if entry and current else 0.0
            # ``current_price`` can legitimately be None — sync_positions()
            # in scripts/live_position_sync.py sets it to None whenever the
            # ticker fetch for that symbol fails (network hiccup, etc.), a
            # normal/expected occurrence in LIVE mode. compute_snapshot()
            # does ``current_price * qty`` directly with no None-guard
            # (dict.get's default only kicks in when the key is *missing*,
            # not when it's present-but-None), so an un-coerced None here
            # crashed every account-figures command with a TypeError.
            # Fall back to entry_price (best estimate of value) or 0.0.
            safe_current = (
                current if current is not None
                else (entry if entry is not None else 0.0)
            )
            normalized.append({
                **p,
                "current_price": safe_current,
                "remaining_qty": qty,
                "unrealized_pnl": unrealized,
                "status": "OPEN",
            })
        return normalized

    def _read_balance_pb(self) -> dict[str, Any]:
        return self._read_json("paper_balance.json")

    # ------------------------------------------------------------------
    #  Canonical accounting computation — single source of truth
    # ------------------------------------------------------------------

    @staticmethod
    def compute_snapshot(
        cash: float,
        realized_pnl: float,
        initial_balance: float,
        open_positions: list[dict[str, Any]],
        total_trades: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
        win_rate: float = 0.0,
        profit_factor: float = 0.0,
        gross_profit: float = 0.0,
        gross_loss: float = 0.0,
    ) -> AccountSnapshot:
        """Canonical computation of ALL derived accounting metrics.

        This is the SINGLE function every component must use:
          - startup reconcile
          - Health Monitor
          - /wallet, /balance
          - Telegram report
          - paper_balance.json writers

        Parameters are raw inputs — no pre-computed derived values.

    Invariants (always true, by construction):
        equity == balance + position_market_value
        net_pnl == equity - initial_balance
        return_pct == ((equity - initial_balance) / initial_balance * 100)
        exposure_pct == (position_value / equity * 100) if equity > 0 else 0
    """
        open_count = len(open_positions)

        # Compute from raw position data
        unrealized_pnl = sum(
            p.get("unrealized_pnl", 0.0) for p in open_positions
        )
        position_market_value = sum(
            p.get("current_price", 0) * (p.get("remaining_qty", 0) or p.get("quantity", 0))
            for p in open_positions
        )

        equity = cash + position_market_value
        net_pnl = equity - initial_balance
        position_value = equity - cash
        exposure_pct = (position_value / equity * 100.0) if equity > 0 else 0.0
        total_return_pct = (
            ((equity - initial_balance) / initial_balance * 100.0)
            if initial_balance > 0 else 0.0
        )

        return AccountSnapshot(
            balance=cash,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            net_pnl=net_pnl,
            total_return_pct=total_return_pct,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            open_positions=open_count,
            initial_balance=initial_balance,
            position_value=position_value,
            exposure_pct=exposure_pct,
        )

    # ------------------------------------------------------------------
    #  Account snapshot
    # ------------------------------------------------------------------

    def account(self) -> AccountSnapshot:
        """Authoritative account snapshot, computed from raw data.

        Reads raw data from ``paper_balance.json`` (cash, realized_pnl,
        initial_balance) and ``positions.json`` (open positions), then
        delegates to ``compute_snapshot()`` — the single canonical function.

        In LIVE mode this delegates to ``_account_live()`` instead — see
        that method for why ``paper_balance.json`` must never be used as
        the cash source once the bot is trading real money.
        """
        if self._is_live():
            return self._account_live()

        pb = self._read_balance_pb()
        cash = pb.get("final_balance", 0.0)
        realized = pb.get("realized_pnl", 0.0)
        initial = resolve_initial_balance(pb, self._read_json("paper_state.json"))
        open_positions = self.open_positions()

        return self.compute_snapshot(
            cash=cash,
            realized_pnl=realized,
            initial_balance=initial,
            open_positions=open_positions,
            total_trades=pb.get("total_trades", 0),
            winning_trades=pb.get("winning_trades", 0),
            losing_trades=pb.get("losing_trades", 0),
            win_rate=pb.get("win_rate", 0.0),
            profit_factor=pb.get("profit_factor", 0.0),
            gross_profit=pb.get("gross_profit", 0.0),
            gross_loss=pb.get("gross_loss", 0.0),
        )

    def _account_live(self) -> AccountSnapshot:
        """LIVE-mode account snapshot.

        Cash comes from the injected ``wallet`` (the exchange, via
        ``_LiveWalletAdapter.fetch_balance()``) — never from
        ``paper_balance.json``, which is only ever written to while the
        engine mode is PAPER (see ``OrderManager.sync_paper_state``) and
        would otherwise sit frozen at its last paper value forever,
        which is exactly the bug this fixes: ``/status`` kept showing
        the old paper balance after the bot switched to live trading.

        Open positions come from ``live_positions.json`` (synced from
        the exchange by ``scripts.live_position_sync``), so exposure %
        and unrealized PnL reflect the real account too.

        Realized PnL / win-rate / trade counts come from the LIVE
        trade-history ledger (``data/live_trade_history.jsonl`` — actual
        exchange fills written by ``scripts.live_trade_ledger``), never from
        paper_balance.json. Before the live ledger existed these were left at
        0, so /status reported a real cash balance next to an empty
        performance record.
        """
        cash = 0.0
        if self._wallet is not None:
            try:
                cash = self._wallet.balance
            except Exception:
                cash = 0.0

        initial = self._live_initial_balance()
        open_positions = self.open_positions()

        ledger_trades = self._live_trade_history()
        realized = sum(float(t.get("net_pnl", 0) or 0) for t in ledger_trades)
        total_trades = len(ledger_trades)
        winning = sum(
            1 for t in ledger_trades if float(t.get("net_pnl", 0) or 0) > 0
        )
        losing = total_trades - winning
        gross_profit = sum(
            float(t.get("net_pnl", 0) or 0) for t in ledger_trades
            if float(t.get("net_pnl", 0) or 0) > 0
        )
        gross_loss = abs(sum(
            float(t.get("net_pnl", 0) or 0) for t in ledger_trades
            if float(t.get("net_pnl", 0) or 0) < 0
        ))
        win_rate = round(winning / total_trades * 100.0, 2) if total_trades else 0.0
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        )

        return self.compute_snapshot(
            cash=cash,
            realized_pnl=realized,
            initial_balance=initial,
            open_positions=open_positions,
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            profit_factor=profit_factor,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        )

    def _live_initial_balance(self) -> float:
        """LIVE-mode initial-equity baseline, snapshot once to disk.

        ``net_pnl`` for real trading must equal (current equity) −
        (equity when live trading first started). Resolving the initial
        figure from ``paper_state.json`` here is wrong: that file is a
        paper-trading artifact (it can even hold leftover test positions,
        e.g. a ``BTC/USDT`` seed with initial_balance=10000), which made
        LIVE HEALTH/``/status`` net_pnl flap between a stale paper figure
        and ``0 − initial`` whenever the exchange balance fetch hiccupped.

        The baseline is captured once (first LIVE-mode read) and persisted
        to ``data/live_initial_balance.json`` so every consumer — HEALTH,
        /status, /wallet, /portfolio — agrees forever.
        """
        path = os.path.join(self._data_dir, "live_initial_balance.json")
        try:
            with open(path) as f:
                return float(json.load(f).get("initial_balance") or 0.0)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            pass

        equity: Optional[float] = None
        if self._wallet is not None:
            try:
                equity = float(self._wallet.equity or 0.0)
            except Exception:
                equity = None
        if equity is None or equity <= 0.0:
            equity = float(os.getenv("ACCOUNT_BALANCE", "0") or 0)
        record = {
            "initial_balance": round(equity, 2),
            "snapshotted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(record, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
        return equity

    # ------------------------------------------------------------------
    #  Positions
    # ------------------------------------------------------------------

    def open_positions_count(self) -> int:
        return len(self.open_positions())

    def open_positions(self) -> list[dict[str, Any]]:
        """OPEN positions = ``positions.json`` (PAPER) / ``live_positions.json``
        (LIVE), filtered to truly-OPEN status AND to this account's quote
        currency (legacy mismatched-quote positions are dropped + warned)."""
        out = []
        for p in self._read_positions():
            if not is_open(p.get("status")):
                continue
            sym = p.get("symbol", "")
            if not self._passes_quote_filter(sym):
                logger.warning(
                    "[LEGACY-DATA] ignored mismatched quote position: "
                    "%s (account quote=%s)",
                    sym, self._account_quote,
                )
                continue
            out.append(p)
        return out

    def all_positions(self) -> list[dict[str, Any]]:
        return self._read_positions()

    # ------------------------------------------------------------------
    #  Orders / Trade history
    # ------------------------------------------------------------------

    def closed_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._read_orders() if o.get("status") == "CLOSED"]

    def buy_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._read_orders() if o.get("side") == "BUY"]

    def trade_history(self) -> list[dict[str, Any]]:
        """CLOSED TRADES — single source of truth, mode-aware.

        PAPER mode: ``paper_trade_history.csv`` (unchanged, existing
        behavior). LIVE mode: ``data/live_trade_history.jsonl`` — the
        actual-exchange-fill ledger written by ``scripts.live_trade_ledger``,
        so a live account's history, win rate and realized PnL never mix
        with the paper ledger and never use price estimates.

        Every completed trade is one row; ``/history``, ``/summary`` and all
        derived accounting (win rate, closed-trades count, realized PnL) read
        from HERE so they can never disagree. Legacy positions whose quote
        currency differs from the account (e.g. ``BTC/USDT`` on an IDR
        account) are excluded and a ``[LEGACY-DATA]`` warning is emitted so
        they never corrupt the report.

        If the canonical CSV has never been written (e.g. a fresh account or
        a data dir migrated from a pre-CSV version), it is materialized once
        from the authoritative ledger (``paper_state.json``) so callers always
        read from exactly one file.
        """
        if self._is_live():
            return self._live_trade_history()

        path = os.path.join(self._data_dir, "paper_trade_history.csv")
        if not os.path.exists(path):
            try:
                from scripts.paper_state_lock import rebuild_trade_history_csv
                rebuild_trade_history_csv(self._data_dir)
            except Exception:
                pass
        trades: list[dict[str, Any]] = []
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = (row.get("symbol") or "").strip()
                    if not sym:
                        continue
                    if not self._passes_quote_filter(sym):
                        logger.warning(
                            "[LEGACY-DATA] ignored mismatched quote trade: "
                            "%s (account quote=%s)",
                            sym, self._account_quote,
                        )
                        continue
                    entry_time = row.get("filled_at") or row.get("created_at", "")
                    exit_time = row.get("closed_at", "")
                    try:
                        net_pnl = float(row.get("net_pnl", 0) or 0)
                    except (ValueError, TypeError):
                        net_pnl = 0.0
                    try:
                        net_pnl_pct = float(row.get("net_pnl_pct", 0) or 0)
                    except (ValueError, TypeError):
                        net_pnl_pct = 0.0
                    try:
                        qty = float(row.get("quantity", 0) or 0)
                    except (ValueError, TypeError):
                        qty = 0.0

                    holding_hours = 0.0
                    if entry_time and exit_time:
                        try:
                            fmt = "%Y-%m-%dT%H:%M:%S.%f"
                            et = datetime.strptime(
                                entry_time.split("+")[0].split("Z")[0], fmt)
                            xt = datetime.strptime(
                                exit_time.split("+")[0].split("Z")[0], fmt)
                            holding_hours = round(
                                (xt - et).total_seconds() / 3600, 2)
                        except (ValueError, IndexError):
                            pass

                    trades.append({
                        "symbol": sym,
                        "side": "SELL",
                        "entry_price": float(row.get("entry_price", 0) or 0),
                        "exit_price": float(row.get("exit_price", 0) or 0),
                        "quantity": qty,
                        "entry_time": entry_time,
                        "exit_time": exit_time,
                        "holding_hours": holding_hours,
                        "net_pnl": net_pnl,
                        "net_pnl_pct": net_pnl_pct,
                        "reason": row.get("exit_reason")
                        or row.get("reason") or "TP/SL",
                        "result": "WIN" if net_pnl >= 0 else "LOSS",
                    })
        except (FileNotFoundError, OSError):
            return []

        trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
        return trades

    def _live_trade_history(self) -> list[dict[str, Any]]:
        """LIVE closed trades from the actual-fill ledger
        (``data/live_trade_history.jsonl``).

        Mirrors the PAPER CSV row shape so every downstream consumer
        (computed metrics, /history, /summary, /performance, daily report)
        needs no mode-specific code.
        """
        trades: list[dict[str, Any]] = []
        try:
            from scripts.live_trade_ledger import load_live_trades  # noqa: PLC0415
            for rec in load_live_trades():
                sym = rec.get("symbol", "")
                if not sym:
                    continue
                if not self._passes_quote_filter(sym):
                    logger.warning(
                        "[LEGACY-DATA] ignored mismatched quote trade: "
                        "%s (account quote=%s)",
                        sym, self._account_quote,
                    )
                    continue
                holding = float(rec.get("holding_duration", 0) or 0)
                net_pnl = float(rec.get("net_pnl", 0) or 0)
                trades.append({
                    "symbol": sym,
                    "side": "SELL",
                    "entry_price": float(rec.get("entry_price", 0) or 0),
                    "exit_price": float(rec.get("exit_price", 0) or 0),
                    "quantity": float(rec.get("quantity", 0) or 0),
                    "entry_time": rec.get("opened_at", ""),
                    "exit_time": rec.get("closed_at", ""),
                    "holding_hours": round(holding / 3600.0, 2) if holding > 0 else 0.0,
                    "net_pnl": net_pnl,
                    "net_pnl_pct": float(rec.get("net_pnl_pct", 0) or 0),
                    "reason": rec.get("exit_reason") or "TP/SL",
                    "result": "WIN" if net_pnl >= 0 else "LOSS",
                    "trade_id": rec.get("trade_id", ""),
                })
        except Exception:
            return []
        trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
        return trades

    # ------------------------------------------------------------------
    #  Computed metrics
    # ------------------------------------------------------------------

    def computed(self, trades: Optional[list[dict[str, Any]]] = None) -> ComputedMetrics:
        if trades is None:
            trades = self.trade_history()

        if not trades:
            return ComputedMetrics()

        total = len(trades)
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) <= 0]

        winning_pnls = [t["net_pnl"] for t in wins]
        losing_pnls = [abs(t["net_pnl"]) for t in losses]

        avg_holding = sum(t.get("holding_hours", 0) for t in trades) / total
        avg_roi = sum(t.get("net_pnl_pct", 0) for t in trades) / total
        avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
        avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
        largest_win = max(winning_pnls) if winning_pnls else 0.0
        largest_loss = max(losing_pnls) if losing_pnls else 0.0
        expectancy = (sum(winning_pnls) - sum(losing_pnls)) / total

        # Streaks
        win_streak = 0
        loss_streak = 0
        current_win = 0
        current_loss = 0
        for t in reversed(trades):
            if t.get("net_pnl", 0) > 0:
                current_win += 1
                current_loss = 0
            else:
                current_loss += 1
                current_win = 0
            win_streak = max(win_streak, current_win)
            loss_streak = max(loss_streak, current_loss)

        best = max(trades, key=lambda t: t.get("net_pnl", 0))
        worst = min(trades, key=lambda t: t.get("net_pnl", 0))

        return ComputedMetrics(
            avg_holding_hours=avg_holding,
            avg_roi=avg_roi,
            avg_win=avg_win,
            avg_loss=avg_loss,
            largest_win=largest_win,
            largest_loss=largest_loss,
            expectancy=expectancy,
            win_streak=win_streak,
            loss_streak=loss_streak,
            best_trade=best,
            worst_trade=worst,
        )

    # ------------------------------------------------------------------
    #  Daily / Weekly / Monthly filters
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(ts: str) -> Optional[datetime]:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                clean = ts.split("+")[0].split("Z")[0]

                # Every exit_time in this codebase is written as UTC
                # (``datetime.now(timezone.utc).isoformat()`` — see
                # OrderManager._sync_paper_files), but stripping the
                # "+00:00"/"Z" suffix above (needed so strptime can
                # match the plain formats) also strips that fact,
                # producing a naive datetime. trades_since() compares
                # this against datetime.now(timezone.utc) (aware) —
                # naive vs aware comparison raises TypeError. Re-attach
                # UTC explicitly so every _parse_ts() result is aware,
                # matching the cutoff it's compared against.

                return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
        return None

    def trades_since(self, cutoff: datetime) -> list[dict[str, Any]]:
        return [
            t for t in self.trade_history()
            if t.get("exit_time") and (
                dt := self._parse_ts(t["exit_time"])
            ) and dt >= cutoff
        ]

    def today_trades(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.trades_since(today_start)

    @staticmethod
    def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(trades)
        wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("net_pnl", 0) <= 0)
        pnl = sum(t.get("net_pnl", 0) for t in trades)
        roi = sum(t.get("net_pnl_pct", 0) for t in trades)
        gross_profit = sum(t["net_pnl"] for t in trades if t.get("net_pnl", 0) > 0)
        gross_loss = abs(sum(t["net_pnl"] for t in trades if t.get("net_pnl", 0) <= 0))
        avg_hold = sum(t.get("holding_hours", 0) for t in trades) / total if total else 0.0
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else (0.0 if gross_profit == 0 else 0.0)
        )
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total else 0.0,
            "pnl": pnl,
            "roi": roi,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "avg_holding_hours": avg_hold,
        }

    def today_summary(self) -> dict[str, Any]:
        return self._summarize_trades(self.today_trades())

    def trades_since_wib_midnight(self) -> list[dict[str, Any]]:
        """Trades closed since the most recent 00:00 WIB (Asia/Jakarta, UTC+7).

        The daily report fires at 00:00 WIB, but ``today_trades()`` cuts
        off at 00:00 UTC — a trade closed between 00:00 and 07:00 WIB
        (= 17:00-24:00 UTC the previous day) would be missing from that
        report. This variant shifts the day boundary so a full WIB day
        maps to its correct UTC instants.

        ``today_trades()``/``today_summary()`` keep the UTC boundary on
        purpose: /wallet, /status and the service-container adapter use
        them, and swapping their base would silently change what those
        commands report as "today".
        """
        now_wib = datetime.now(_WIB_TZ)
        wib_midnight = now_wib.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.trades_since(wib_midnight.astimezone(timezone.utc))

    def today_summary_wib(self) -> dict[str, Any]:
        """Today's trade summary using the WIB (UTC+7) day boundary.

        Mirror of ``today_summary()`` for the 00:00 WIB daily report —
        includes every trade closed during the current WIB calendar day.
        """
        return self._summarize_trades(self.trades_since_wib_midnight())

    # ------------------------------------------------------------------
    #  Scanner / market helpers
    # ------------------------------------------------------------------

    def scanner_results(self) -> dict[str, Any]:
        return self._read_json("scanner_results.json")

    # ------------------------------------------------------------------
    #  Volume formatting
    # ------------------------------------------------------------------

    @staticmethod
    def fmt_volume(val: float) -> str:
        if val >= 1_000_000:
            return f"{val / 1_000_000:.2f}M"
        if val >= 1_000:
            return f"{val / 1_000:.2f}K"
        return f"{val:.2f}"
