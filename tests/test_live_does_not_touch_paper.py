"""Regression tests for BUG-3: LIVE mode must never modify paper accounting.

Before the fix, the LIVE position monitor's closure handler
(``main._monitor_live_closure``) called ``_update_paper_on_closure`` — the
SAME routine the PAPER path uses — which wrote ``data/paper_balance.json``,
``data/paper_orders.json`` and ``data/paper_state.json`` while running
against a real exchange. In LIVE mode those files belong to the PAPER
account and must never change; the real account's PnL and balance live on
the exchange and in ``data/live_positions.json``.

Fix: the LIVE closure path computes PnL from the reconciled position (real
fill data) and the "Balance now ..." notification line from the real
exchange balance. It never reads or writes any paper state file. The PAPER
path is untouched and still updates the paper accounting files.

These tests never touch a real exchange or the real ``data/`` dir: every
``data/...`` path is redirected into ``tmp_path`` via ``monkeypatch.chdir``
and CCXT is a recording fake.
"""

import json
import logging
import os
from typing import Any

import pytest

import main as main_mod
import scripts.protection_manager as protection_manager
from scripts.execution_engine import LiveExecutor
from scripts.execution_pipeline import ExecutionPipeline
from scripts.execution_provider import LiveExecutionProvider

SYMBOL = "BTC/USDT"


class _RecordingExchange:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self._guard = __import__("threading").Lock()

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            self.orders.append(kwargs)
        return {
            "id": f"bug3{len(self.orders)}",
            "status": "closed",
            "filled": kwargs["amount"],
            "average": float(kwargs.get("price") or 0) or 100.0,
            "price": kwargs.get("price") or 100.0,
            "cost": float(kwargs["amount"]) * 100.0,
            "fee": {"cost": 0.0},
        }


class _FakeProvider:
    def __init__(self, exchange: _RecordingExchange) -> None:
        self._ex = exchange

    def _get_exchange(self) -> _RecordingExchange:
        return self._ex

    def fetch_balance(self) -> dict[str, Any]:
        return {"free": {"USDT": 100000.0}}

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": 100.0, "ask": 100.0, "bid": 100.0}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        return price

    def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
        return {}

    def market_buy_requires_price(self) -> bool:
        return False


class _FakeExchangeManager:
    name = "binance"

    def __init__(self, provider: _FakeProvider, balance: dict[str, Any]) -> None:
        self._provider = provider
        self._balance = balance

    def get_provider(self) -> _FakeProvider:
        return self._provider

    def fetch_balance(self) -> dict[str, Any]:
        return self._balance


class _FakeConfig:
    quote_currency = "USDT"
    exchange = "binance"


class _FakeOrderManager:
    mode = "LIVE"

    def is_live_enabled(self) -> bool:
        return True


class _FakeContainer:
    def __init__(self, exchange: _FakeExchangeManager) -> None:
        self.exchange = exchange
        self.order = _FakeOrderManager()
        self._config = _FakeConfig()


class _FakeProtectionManager:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_protection(self, symbol: str, reason: str = "manual") -> None:
        self.cancelled.append(symbol)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.closed: list[dict[str, Any]] = []

    def notify_position_closed(self, **kwargs: Any) -> bool:
        self.closed.append(kwargs)
        return True


class _FakeBinance:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._tickers = {SYMBOL: {"last": 105.0}}

    def fetch_tickers(self, symbols: Any = None) -> dict[str, Any]:
        return self._tickers


def _make_pipeline(exchange: _RecordingExchange) -> tuple[ExecutionPipeline, _FakeExchangeManager]:
    manager = _FakeExchangeManager(
        _FakeProvider(exchange),
        {"USDT": {"free": 1000.0, "used": 0.0, "total": 1234.0}},
    )
    provider = LiveExecutionProvider(manager, _FakeConfig())
    return ExecutionPipeline(provider), manager


def _base_position(**overrides: Any) -> dict[str, Any]:
    pos = {
        "symbol": SYMBOL,
        "status": "OPEN",
        "entry_price": 110.0,
        "quantity": 0.1,
        "remaining_qty": 0.1,
        "cost_basis": 11.0,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "stop_loss": 110.0,
        "tp1_hit": False,
        "realized_pnl": 0.0,
        "floating_pnl_pct": 0.0,
        "holding_hours": 1.0,
    }
    pos.update(overrides)
    return pos


def _seed_position(position: dict[str, Any]) -> None:
    from scripts.exit_gate import save_position  # noqa: PLC0415

    save_position(position["symbol"], position)


def _sell_orders(exchange: _RecordingExchange) -> list[dict[str, Any]]:
    return [o for o in exchange.orders if o.get("side") == "sell"]


def _read_positions() -> dict[str, Any]:
    with open("data/positions.json") as f:
        return json.load(f)


PAPER_FILES = (
    "data/paper_balance.json",
    "data/paper_orders.json",
    "data/paper_state.json",
    "data/paper_trade_history.csv",
)


@pytest.fixture
def _paper_env(tmp_path, monkeypatch) -> None:
    """Redirect every ``data/...`` path into ``tmp_path/data`` and reset the
    process-global live-arm flag, so the tests never touch the real data dir."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


_LOGGER = logging.getLogger("test_bug3")


class TestLiveMonitorNeverTouchesPaper:
    def test_live_monitor_closure_preserves_paper_files(self, _paper_env, monkeypatch) -> None:
        """End-to-end: ``_monitor_positions`` in LIVE mode fires the SL exit
        (one real sell), records ``closure_notified`` in positions.json, and
        leaves every paper accounting file ABSENT — they must never be
        created/updated by a LIVE path."""
        import ccxt  # noqa: PLC0415

        exchange = _RecordingExchange()
        _, manager = _make_pipeline(exchange)
        container = _FakeContainer(manager)
        fake_pm = _FakeProtectionManager()
        notifier = _RecordingNotifier()

        monkeypatch.setattr(ccxt, "binance", _FakeBinance)
        monkeypatch.setattr(protection_manager, "ProtectionManager", lambda *a, **k: fake_pm)

        _seed_position(_base_position())

        for f in PAPER_FILES:
            assert not os.path.exists(f), f"precondition: {f} must be absent"

        LiveExecutor.enable()
        try:
            main_mod._monitor_positions(_LOGGER, notifier, None, container=container)
        finally:
            LiveExecutor.disable()

        sells = _sell_orders(exchange)
        assert len(sells) == 1, f"expected exactly one SL sell, got {len(sells)}"
        assert sells[0]["amount"] == pytest.approx(0.1)

        data = _read_positions()
        assert len(data.get("positions", [])) == 1
        final = data["positions"][0]
        assert final["status"] in ("CLOSED", "STOPPED")
        assert final["remaining_qty"] == pytest.approx(0.0)
        assert final.get("closure_notified") is True

        assert fake_pm.cancelled == [SYMBOL], "protection must be cancelled before the sell"

        assert notifier.closed, "LIVE close notification must still be sent"
        assert notifier.closed[0]["balance"] == pytest.approx(1234.0)
        assert notifier.closed[0]["pnl"] == pytest.approx(final.get("total_pnl", 0))

        for f in PAPER_FILES:
            assert not os.path.exists(f), f"LIVE must never create {f}"

    def test_live_closure_direct_uses_reconciled_pnl_and_live_balance(
        self, _paper_env, monkeypatch,
    ) -> None:
        """Unit: ``_monitor_live_closure`` computes PnL from the reconciled
        position (never paper files) and balance from the exchange, persists
        ``closure_notified`` atomically, and does not call the paper closure
        routine."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            main_mod, "_update_paper_on_closure",
            lambda *a: calls.append(a) or (0.0, 0.0),
        )

        _, manager = _make_pipeline(_RecordingExchange())
        container = _FakeContainer(manager)
        notifier = _RecordingNotifier()

        reconciled = _base_position(
            status="STOPPED",
            remaining_qty=0.0,
            total_pnl=-1.75,
            realized_pnl=-1.75,
            current_price=105.0,
        )
        _seed_position(reconciled)

        main_mod._monitor_live_closure(
            _LOGGER, notifier, container, SYMBOL, 105.0,
            _base_position(), reconciled,
        )

        assert calls == [], "_update_paper_on_closure must never run in LIVE mode"
        assert notifier.closed, "notification must be sent"
        assert notifier.closed[0]["pnl"] == pytest.approx(-1.75)
        assert notifier.closed[0]["balance"] == pytest.approx(1234.0)

        final = _read_positions()["positions"][0]
        assert final.get("closure_notified") is True

        for f in PAPER_FILES:
            assert not os.path.exists(f)


class TestLiveQuoteBalance:
    def test_missing_exchange_falls_back_to_zero(self, _paper_env) -> None:
        assert main_mod._live_quote_balance(_FakeContainer(None)) == 0.0

    def test_exchange_error_falls_back_to_zero(self, _paper_env) -> None:
        class _Broken:
            def fetch_balance(self) -> dict[str, Any]:
                raise RuntimeError("exchange down")

        container = _FakeContainer(_Broken())
        assert main_mod._live_quote_balance(container) == 0.0


class TestPaperMonitorStillUpdatesPaper:
    def test_paper_closure_still_writes_paper_files(self, _paper_env, monkeypatch) -> None:
        """Control: the PAPER path must keep updating the paper accounting
        files — the BUG-3 fix must not have broken paper mode."""
        import ccxt  # noqa: PLC0415

        notifier = _RecordingNotifier()
        monkeypatch.setattr(ccxt, "binance", _FakeBinance)

        _seed_position(_base_position())

        main_mod._monitor_positions(_LOGGER, notifier, None, container=None)

        data = _read_positions()
        assert data["positions"][0]["status"] in ("CLOSED", "STOPPED")

        assert os.path.exists("data/paper_balance.json"), "paper_balance.json must be written in PAPER mode"
        assert os.path.exists("data/paper_orders.json"), "paper_orders.json must be written in PAPER mode"

        with open("data/paper_balance.json") as f:
            pb = json.load(f)
        assert float(pb.get("final_balance", 0)) > 0.0
        assert notifier.closed, "paper close notification must still be sent"
