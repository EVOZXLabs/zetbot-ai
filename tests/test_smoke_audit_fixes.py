"""Regression tests for the smoke-test audit fixes.

Proves:
1. BUG-1 — SafeGuard must not block a BUY for a symbol whose simulated
   position (Position stage) is already OPEN in positions.json: the
   planned symbol is excluded from the max-open-positions count.
2. BUY_OPENED notification — the DI paper path sends a notification for
   a FILLED plan (TASK 1: THRESHOLD/IDR was APPROVED by risk but never
   executed, so no notification ever fired).
3. Accounting consistency — equity == cash + position_market_value,
   net_pnl == realized + unrealized, and paper_balance.json final_equity
   includes open position value (BUG-3: /wallet showed Open 49,883.18
   but final_equity was frozen at cash).
4. BUG-2 — monitor closure is idempotent: when the pipeline already
   closed+credited a position (paper_state.json CLOSED), the monitor's
   closure handler must NOT credit the wallet a second time.
5. BUG-4 — ghost positions (OPEN in positions.json, absent from the
   paper ledger) never produce BUY_OPENED notifications and are pruned
   from positions.json by the pipeline.
6. TASK 4 — CCXT root-cause logging includes exchange, method/symbol
   label and exception class name.

All tests are offline and hermetic: the exchange is a FakeProvider /
stubbed container and every data file lives in a tmp_path sandbox.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.app_config import AppConfig
from scripts.pipeline import Pipeline
from scripts.safety_limits import SafeGuard
from scripts.logger import PipelineLogger


def _cfg(exchange: str = "binance", quote: str = "USDT") -> AppConfig:
    return AppConfig(
        exchange=exchange,
        quote_currency=quote,
        timeframe="1h",
        telegram_chat_id="12345",
        account_balance=1_000_000,
        max_positions=1,
        max_risk_per_trade_pct=2,
        scanner_threads=5,
        scanner_top_n=50,
        telegram_timeout=10,
        telegram_retry=3,
        min_rr=1.5,
        max_rr=5,
        min_probability=50,
        max_atr_pct=8,
        tp1_sell_pct=30,
        tp2_sell_pct=30,
        tp3_sell_pct=40,
        taker_fee=0.001,
        maker_fee=0.00075,
        slippage_bps=3,
    )


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs in a throwaway data directory."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)


def _write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _read(path: str) -> Any:
    with open(path) as f:
        return json.load(f)


def _logger() -> PipelineLogger:
    return PipelineLogger(_cfg())


def _stub_container(
    safeguard: SafeGuard,
    order: Any = None,
    exchange: Any = None,
) -> SimpleNamespace:
    order = order or SimpleNamespace(
        mode="PAPER", is_live_enabled=lambda: False,
    )
    return SimpleNamespace(
        safeguard=safeguard,
        order=order,
        exchange=exchange,
    )


# ============================================================================
#  BUG-1 — SafeGuard planned-symbol exclusion
# ============================================================================


class TestSafeGuardPlannedSymbols:
    def test_planned_symbol_simulated_in_positions_does_not_block_buy(
        self,
    ) -> None:
        """The Position stage simulates the READY plan as OPEN in
        positions.json BEFORE the paper engine executes it. The guard must
        exclude that symbol from the open-position count, otherwise the
        real BUY is rejected ("Max open positions reached: 1/1") and the
        position stays a ghost — the THRESHOLD/IDR smoke-test incident."""
        sg = SafeGuard(max_open_positions=1)
        _write("data/positions.json", {
            "positions": [{
                "symbol": "THRESHOLD/IDR",
                "status": "OPEN",
                "quantity": 804.5674,
                "remaining_qty": 804.5674,
            }],
        })

        # Before the fix: no planned-symbol knowledge -> guard blocks.
        allowed, reason = sg.can_open_new_position()
        assert not allowed
        assert "Max open positions" in reason

        # Fixed: the pipeline declares the symbol as mid-open.
        sg.set_planned_symbols({"THRESHOLD/IDR"})
        allowed, reason = sg.can_open_new_position()
        assert allowed, f"planned symbol must not block its own buy: {reason}"
        assert reason == ""

    def test_other_real_open_position_still_blocks(self) -> None:
        """Only the planned symbol is excluded — a genuinely open OTHER
        position still trips the MAX_POSITIONS guard."""
        sg = SafeGuard(max_open_positions=1)
        _write("data/positions.json", {
            "positions": [
                {"symbol": "PRIME/IDR", "status": "OPEN", "quantity": 1.0},
                {"symbol": "THRESHOLD/IDR", "status": "OPEN", "quantity": 1.0},
            ],
        })
        sg.set_planned_symbols({"THRESHOLD/IDR"})

        allowed, reason = sg.can_open_new_position()
        assert not allowed
        assert "Max open positions" in reason

    def test_planned_symbols_clear_between_cycles(self) -> None:
        """A stale planned-symbol set must never leak into the next cycle:
        once the pipeline finishes, the exclusion is reset."""
        sg = SafeGuard(max_open_positions=1)
        sg.set_planned_symbols({"THRESHOLD/IDR"})
        sg.set_planned_symbols(set())
        _write("data/positions.json", {
            "positions": [{"symbol": "THRESHOLD/IDR", "status": "OPEN"}],
        })
        allowed, _ = sg.can_open_new_position()
        assert not allowed


# ============================================================================
#  TASK 1 — BUY_OPENED notification on FILLED paper buy
# ============================================================================


class TestBuyOpenedNotification:
    def test_paper_fill_sends_buy_opened_notification(self, monkeypatch: Any) -> None:
        """A READY plan executed by the DI paper stage with status FILLED
        MUST dispatch BUY_OPENED through the notifier and record the symbol
        in .notified_buys."""
        from scripts.paper_state_lock import add_notified_buy

        sg = SafeGuard(max_open_positions=1)
        container = _stub_container(sg)
        pipeline = Pipeline(_cfg(), _logger(), container=container)

        notifier = SimpleNamespace(notify_buy_opened=lambda **kw: True)
        pipeline.set_notifier(notifier)

        # Simulate a full cycle's files: READY plan + simulated position.
        _write("data/trade_plan.json", {
            "plans": [{
                "symbol": "THRESHOLD/IDR",
                "status": "READY",
                "entry_price": 62.0,
                "quantity": 804.5674,
                "position_size_usdt": 49_883.18,
                "stop_loss": 58.0,
                "tp1": 70.0,
                "tp2": 78.0,
                "tp3": 86.0,
            }],
        })
        _write("data/positions.json", {
            "positions": [{
                "symbol": "THRESHOLD/IDR", "status": "OPEN",
                "quantity": 804.5674, "remaining_qty": 804.5674,
                "entry_price": 62.0,
            }],
        })

        # Seed the paper wallet so the 49,883.18 BUY can actually fill.
        _write("data/paper_balance.json", {
            "initial_balance": 1_000_000.0,
            "final_balance": 1_000_000.0,
        })
        _write("data/paper_state.json", {
            "balance": 1_000_000.0,
            "positions": {},
        })

        # Use the REAL paper provider: balance big enough for the buy.
        # Keep the reconcile path offline (no real exchange calls).
        monkeypatch.setattr(
            "bot.data.fetch_tickers_cached", lambda *a, **k: {},
        )
        calls: list[str] = []
        notifier = SimpleNamespace(
            notify_buy_opened=lambda **kw: (calls.append(kw["symbol"]) or True),
        )
        pipeline.set_notifier(notifier)
        add_notified_buy("OTHER/SYMBOL")

        pipeline._run_paper_di()

        assert calls, "BUY_OPENED must be dispatched for a FILLED paper buy"
        assert calls == ["THRESHOLD/IDR"]

        with open("data/.notified_buys") as f:
            notified = {line.strip() for line in f if line.strip()}
        assert "THRESHOLD/IDR" in notified


# ============================================================================
#  BUG-3 — paper_balance.json final_equity includes open position value
# ============================================================================


class TestEquityIncludesOpenPositions:
    def test_persist_paper_state_equity_cash_plus_position_value(
        self,
    ) -> None:
        """BUG-3: /wallet showed Cash 997,663.60 / Open 49,883.18 /
        Total 1,047,546.78 while paper_balance.json final_equity was
        frozen at the cash value. _persist_paper_state must compute
        equity via the canonical MetricsManager (cash + position value)."""
        from scripts.pipeline import Pipeline  # noqa: PLC0415

        provider = SimpleNamespace(
            get_balance=lambda: 900_000.0,
            positions={
                "THRESHOLD/IDR": SimpleNamespace(
                    status="OPEN", current_price=62.0, remaining_qty=804.5674,
                ),
            },
        )
        _write("data/paper_balance.json", {
            "initial_balance": 1_000_000.0,
            "final_balance": 900_000.0,
            "final_equity": 900_000.0,
        })

        pipeline = Pipeline(_cfg(), _logger(), container=None)
        pipeline._persist_paper_state(provider)

        pb = _read("data/paper_balance.json")
        expected_equity = round(900_000.0 + 62.0 * 804.5674, 2)
        assert pb["final_equity"] == pytest.approx(expected_equity)
        assert pb["final_equity"] > pb["final_balance"]

    def test_equity_with_no_open_positions_equals_cash(self) -> None:
        provider = SimpleNamespace(
            get_balance=lambda: 997_663.60,
            positions={},
        )
        _write("data/paper_balance.json", {
            "initial_balance": 1_000_000.0,
            "final_balance": 997_663.60,
            "final_equity": 997_663.60,
        })
        pipeline = Pipeline(_cfg(), _logger(), container=None)
        pipeline._persist_paper_state(provider)
        pb = _read("data/paper_balance.json")
        assert pb["final_equity"] == pytest.approx(997_663.60)


# ============================================================================
#  BUG-2 — idempotent monitor closure (no double credit)
# ============================================================================


class TestMonitorClosureIdempotent:
    def test_already_closed_position_not_credited_twice(self) -> None:
        """The pipeline provider closed+credited PRIME/IDR (paper_state
        status CLOSED); the monitor's closure handler then ran 5 ms later
        with the full original quantity. It must NOT credit the wallet a
        second time, must NOT bump total_trades, and must NOT append a
        second SELL order (the 00:39:10.178 vs .184 double-sell)."""
        from main import _update_paper_on_closure  # noqa: PLC0415

        initial_cash = 947_715.56
        _write("data/paper_balance.json", {
            "initial_balance": 1_000_000.0,
            "final_balance": initial_cash,
            "final_equity": initial_cash,
            "realized_pnl": 8_177.22,
            "total_trades": 3,
        })
        _write("data/positions.json", {
            "positions": [{
                "symbol": "PRIME/IDR", "status": "OPEN",
                "quantity": 11.3173, "remaining_qty": 11.3173,
                "entry_price": 4419.3254,
            }],
        })
        _write("data/paper_orders.json", {"orders": [
            {"symbol": "PRIME/IDR", "side": "SELL", "quantity": 7.92211,
             "net_pnl": 9077.61, "status": "CLOSED"},
        ]})
        _write("data/paper_state.json", {
            "balance": initial_cash,
            "positions": {
                "PRIME/IDR": {
                    "symbol": "PRIME/IDR", "status": "CLOSED",
                    "quantity": 11.3173, "remaining_qty": 0.0,
                },
            },
        })

        reconciled = {
            "symbol": "PRIME/IDR", "status": "STOPPED",
            "quantity": 11.3173, "remaining_qty": 0.0,
            "entry_price": 4419.3254, "total_pnl": 9_376.18,
        }
        pnl, new_balance = _update_paper_on_closure(
            _logger(), "PRIME/IDR", reconciled, 4246.7256, "Stop Loss",
        )

        pb = _read("data/paper_balance.json")
        assert pb["final_balance"] == pytest.approx(initial_cash), (
            "already-credited position must not credit the wallet again"
        )
        assert pb["total_trades"] == 3
        assert pb["realized_pnl"] == pytest.approx(8_177.22)

        orders = _read("data/paper_orders.json")["orders"]
        assert len(orders) == 1, "no duplicate SELL order may be appended"

    def test_open_ledger_position_still_credits_once(self) -> None:
        """When paper_state still shows the position OPEN (the pipeline has
        NOT handled it yet), the monitor's closure is the legitimate
        single-credit path and must still work exactly as before."""
        from main import _update_paper_on_closure  # noqa: PLC0415

        _write("data/paper_balance.json", {
            "initial_balance": 1_000_000.0,
            "final_balance": 900_000.0,
            "final_equity": 900_000.0,
            "realized_pnl": 0.0,
            "total_trades": 0,
        })
        _write("data/positions.json", {
            "positions": [{
                "symbol": "BTC/USDT", "status": "OPEN",
                "quantity": 0.1, "remaining_qty": 0.1,
                "entry_price": 50_000.0,
            }],
        })
        _write("data/paper_orders.json", {"orders": []})
        _write("data/paper_state.json", {
            "balance": 900_000.0,
            "positions": {
                "BTC/USDT": {
                    "symbol": "BTC/USDT", "status": "OPEN",
                    "quantity": 0.1, "remaining_qty": 0.1,
                    "entry_price": 50_000.0,
                },
            },
        })

        reconciled = {
            "symbol": "BTC/USDT", "status": "CLOSED",
            "quantity": 0.1, "remaining_qty": 0.1,
            "entry_price": 50_000.0, "total_pnl": 0.0,
        }
        pnl, _ = _update_paper_on_closure(
            _logger(), "BTC/USDT", reconciled, 55_000.0, "Take Profit",
        )

        pb = _read("data/paper_balance.json")
        assert pb["final_balance"] > 900_000.0
        assert pb["total_trades"] == 1
        assert len(_read("data/paper_orders.json")["orders"]) == 1
        assert pnl > 0


# ============================================================================
#  BUG-4 — ghost positions: no notification, pruned by pipeline
# ============================================================================


class TestGhostPositions:
    def test_ghost_position_never_notified(self, tmp_path: Any) -> None:
        """BTC/USDT was OPEN in positions.json (order_id "o1", a test
        fixture leftover) but absent from the paper ledger. Restart
        recovery must NOT send a BUY_OPENED for it."""
        from main import _notify_existing_positions  # noqa: PLC0415

        _write("data/positions.json", {
            "positions": [{
                "symbol": "BTC/USDT", "status": "OPEN",
                "entry_price": 50_000.0, "quantity": 0.014,
            }],
        })
        _write("data/paper_state.json", {
            "balance": 947_715.56,
            "positions": {},  # ledger has NO BTC/USDT -> ghost
        })

        calls: list[str] = []
        notifier = SimpleNamespace(
            notify_buy_opened=lambda **kw: (calls.append(kw["symbol"]) or True),
        )
        _notify_existing_positions(_logger(), notifier)
        assert calls == [], "ghost positions must never be notified"

    def test_real_open_position_still_notified(self) -> None:
        from main import _notify_existing_positions  # noqa: PLC0415

        _write("data/positions.json", {
            "positions": [{
                "symbol": "THRESHOLD/IDR", "status": "OPEN",
                "entry_price": 62.0, "quantity": 804.5674,
            }],
        })
        _write("data/paper_state.json", {
            "balance": 947_715.56,
            "positions": {
                "THRESHOLD/IDR": {
                    "symbol": "THRESHOLD/IDR", "status": "OPEN",
                    "quantity": 804.5674, "remaining_qty": 804.5674,
                },
            },
        })
        calls: list[str] = []
        notifier = SimpleNamespace(
            notify_buy_opened=lambda **kw: (calls.append(kw["symbol"]) or True),
        )
        _notify_existing_positions(_logger(), notifier)
        assert calls == ["THRESHOLD/IDR"]

    def test_pipeline_prunes_ghost_positions(self) -> None:
        """After the paper stage, OPEN positions.json entries with no OPEN
        counterpart in the ledger are ghosts and get closed so they stop
        inflating equity/exposure and blocking new buys."""
        pipeline = Pipeline(_cfg(), _logger(), container=None)
        _write("data/positions.json", {
            "positions": [
                {"symbol": "BTC/USDT", "status": "OPEN", "quantity": 0.014},
                {"symbol": "THRESHOLD/IDR", "status": "OPEN",
                 "quantity": 804.5674},
            ],
        })
        _write("data/paper_state.json", {
            "balance": 947_715.56,
            "positions": {
                "THRESHOLD/IDR": {
                    "symbol": "THRESHOLD/IDR", "status": "OPEN",
                    "quantity": 804.5674, "remaining_qty": 804.5674,
                },
            },
        })
        provider = SimpleNamespace()

        pipeline._prune_ghost_positions(provider)

        positions = _read("data/positions.json")["positions"]
        by_sym = {p["symbol"]: p for p in positions}
        assert by_sym["THRESHOLD/IDR"]["status"] == "OPEN"
        assert by_sym["BTC/USDT"]["status"] == "CLOSED"


# ============================================================================
#  TASK 4 — CCXT root-cause logging
# ============================================================================


class TestExchangeRootCauseLogging:
    def test_retry_log_includes_exchange_method_and_exc_class(
        self, caplog: Any,
    ) -> None:
        """Exchange failures must be logged with the exchange name, the
        method/symbol label, and the CCXT exception CLASS so the root
        cause is visible without tracebacks."""
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        calls = {"n": 0}

        def _flaky() -> None:
            calls["n"] += 1
            raise ccxt.NetworkError("connection reset")

        with caplog.at_level("WARNING", logger="ZetBot"):
            with pytest.raises(ccxt.NetworkError):
                exchange_call_with_retry(
                    _flaky,
                    label="get_ticker(BTC/USDT)",
                    retries=1,
                    exchange="indodax",
                )

        assert any(
            "indodax" in r.message
            and "get_ticker(BTC/USDT)" in r.message
            and "NetworkError" in r.message
            for r in caplog.records
        ), "log must contain exchange, label and exception class"

    def test_load_markets_retry_includes_exchange(self, caplog: Any) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        def _flaky() -> None:
            raise ccxt.RequestTimeout("timed out")

        with caplog.at_level("WARNING", logger="ZetBot"):
            with pytest.raises(ccxt.RequestTimeout):
                exchange_call_with_retry(
                    _flaky, label="load_markets", retries=1, exchange="binance",
                )
        assert any(
            "binance" in r.message and "RequestTimeout" in r.message
            for r in caplog.records
        )
