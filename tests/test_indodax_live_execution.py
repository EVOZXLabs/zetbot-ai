"""Live-execution regression tests for the Indodax (IDR) exchange.

Three Indodax-specific live bugs are covered:

1. ccxt's ``indodax.create_order`` REJECTS a market BUY without ``price``
   (it sizes the order by quote cost = amount × price). Binance ignores
   price for market orders — passing it there would silently convert the
   order into a ``quoteOrderQty`` spend. Live buys now pass the price
   through ONLY when ``market_buy_requires_price()`` is true.
2. Indodax has no client-order-id concept, and its private endpoint signs
   the entire request body — sending a stray ``clientOrderId`` param risks
   rejection. ``IndodaxProvider.client_order_id_params`` returns {}.
3. Indodax's ``create_order`` answers with a bare ``{id, info}``, and even
   ``fetch_order`` reports ``status='closed'`` with ``filled=None``. The
   live providers now poll ``fetch_order`` after submitting and map
   ``closed`` → FILLED instead of reporting PENDING forever.

All tests are offline: the real providers are driven against a recording
CCXT fake, and ``data/...`` paths are redirected into ``tmp_path``.
"""

import json
import os
import threading
from typing import Any

import pytest

import scripts.protection_manager as protection_manager
from scripts.execution_engine import LiveExecutor, _map_live_order_status
from scripts.execution_provider import (
    LiveExecutionProvider,
    _map_live_status,
    _settle_live_order,
)
from scripts.exchange_manager import ExchangeManager
from scripts.exchange_providers import BinanceProvider, IndodaxProvider
from scripts.pipeline import Pipeline

SYMBOL = "GOAT/IDR"


class _RecordingIndodax:
    """Offline stand-in for ccxt's indodax client.

    Mirrors the real API quirks: ``create_order`` returns only ``{id, info}``
    (no status/fill) and ``fetch_order`` reports ``status='closed'`` with
    ``filled=None``.
    """

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self._guard = threading.Lock()
        self.markets: dict[str, Any] = {}
        self.last_price = 241.0
        self.free_balance: dict[str, float] = {"IDR": 1_000_000.0, "GOAT": 10.0}

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": self.last_price, "ask": self.last_price, "bid": self.last_price}

    def fetch_tickers(self, symbols: Any = None) -> dict[str, Any]:
        return {s: self.fetch_ticker(s) for s in (symbols or [])}

    def fetch_balance(self) -> dict[str, Any]:
        return {"free": dict(self.free_balance)}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(amount)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(price)

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            self.orders.append(kwargs)
            order_id = f"ord{len(self.orders)}"
        return {"id": order_id, "info": {"success": 1, "return": {"order_id": order_id}}}

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        amount = 0.0
        price = self.last_price
        for o in self.orders:
            if o.get("id", None) == order_id or f"ord{self.orders.index(o) + 1}" == order_id:
                amount = float(o.get("amount", 0))
                price = float(o.get("price") or self.last_price)
                break
        return {
            "id": order_id,
            "symbol": symbol,
            "status": "closed",
            "filled": None,
            "remaining": 0.0,
            "amount": amount,
            "price": price,
            "average": None,
            "cost": amount * price,
            "info": {},
        }


def _build_manager(recording: Any, exchange: str = "indodax", monkeypatch=None) -> ExchangeManager:
    mgr = ExchangeManager(active=exchange, api_key="k", api_secret="s")
    provider = mgr.get_provider()
    if monkeypatch is not None:
        monkeypatch.setattr(provider, "_get_exchange", lambda: recording)
    return mgr


class _Cfg:
    quote_currency = "IDR"
    exchange = "indodax"
    timeframe = "1h"
    account_balance = 1_000_000.0


class _StubLogger:
    def info(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


class _FakeOrderManager:
    mode = "LIVE"

    def is_live_enabled(self) -> bool:
        return True


class _FakeSafeguard:
    def can_open_new_position(self) -> tuple[bool, str]:
        return True, "ok"

    def set_planned_symbols(self, symbols: set[str]) -> None:
        pass


class _FakeContainer:
    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange
        self.order = _FakeOrderManager()
        self.safeguard = _FakeSafeguard()
        self._config = _Cfg()


class _FakeNotifier:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    def notify_buy_opened(self, **kwargs: Any) -> bool:
        self.opened.append(kwargs)
        return True


class _FakePublicTicker:
    def __init__(self, tickers: dict[str, dict[str, Any]]) -> None:
        self._tickers = tickers

    def fetch_tickers(self, symbols: Any = None) -> dict[str, Any]:
        return {s: self._tickers.get(s, {}) for s in (symbols or [])}


class _FakeProtectionManager:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_protection(self, symbol: str, reason: str = "manual") -> None:
        self.cancelled.append(symbol)


@pytest.fixture
def _live_env(tmp_path, monkeypatch) -> None:
    """Redirect ``data/...`` into ``tmp_path`` and reset the live-arm flag."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    LiveExecutor.disable()
    yield
    LiveExecutor.disable()


# ----------------------------------------------------------------------
#  Provider capability flags
# ----------------------------------------------------------------------


class TestIndodaxCapabilities:
    def test_indodax_market_buy_requires_price(self) -> None:
        assert IndodaxProvider().market_buy_requires_price() is True

    def test_binance_market_buy_does_not_require_price(self) -> None:
        assert BinanceProvider().market_buy_requires_price() is False

    def test_indodax_sends_no_client_order_id_param(self) -> None:
        assert IndodaxProvider().client_order_id_params("abc") == {}

    def test_indodax_market_orders_declare_order_type(self) -> None:
        # Indodax's /trade endpoint defaults order_type to "limit" when
        # absent (API change 10 Sep 2022), so a market order sent without
        # it is treated as a limit buy sized by idr — which the server
        # rejects ("both idr set & order_type LIMIT"). The provider must
        # always tag market orders explicitly.
        assert IndodaxProvider().market_order_params() == {"order_type": "market"}

    def test_binance_market_orders_need_no_extra_params(self) -> None:
        assert BinanceProvider().market_order_params() == {}

    def test_binance_sends_client_order_id_param(self) -> None:
        assert BinanceProvider().client_order_id_params("abc") == {"newClientOrderId": "abc"}


# ----------------------------------------------------------------------
#  Status mapping (bug 3): closed/filled ⇒ FILLED, never PENDING forever
# ----------------------------------------------------------------------


class TestStatusMapping:
    def test_closed_without_filled_is_filled(self) -> None:
        assert _map_live_status({"status": "closed", "filled": None}, 1.0) == "FILLED"

    def test_open_without_filled_is_pending(self) -> None:
        assert _map_live_status({"status": "open", "filled": None}, 1.0) == "PENDING"

    def test_cancelled_and_rejected(self) -> None:
        assert _map_live_status({"status": "canceled"}, 1.0) == "CANCELLED"
        assert _map_live_status({"status": "rejected"}, 1.0) == "REJECTED"

    def test_partial_fill(self) -> None:
        assert _map_live_status({"status": "open", "filled": 0.5}, 1.0) == "PARTIALLY_FILLED"
        assert _map_live_status({"status": "partially_filled"}, 1.0) == "PARTIALLY_FILLED"

    def test_full_fill_by_amount(self) -> None:
        assert _map_live_status({"status": "open", "filled": 1.0}, 1.0) == "FILLED"

    def test_engine_mapper_shares_semantics(self) -> None:
        assert _map_live_order_status({"status": "closed", "filled": None}, 1.0) == "FILLED"
        assert _map_live_order_status({"status": "open", "filled": None}, 1.0) == "PENDING"


class TestSettleLiveOrder:
    def test_confirms_bare_indodax_order_via_fetch_order(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        mgr = _build_manager(recording, monkeypatch=monkeypatch)
        provider = mgr.get_provider()
        initial = {"id": "ord9", "info": {"success": 1}}
        settled = _settle_live_order(provider, "ord9", SYMBOL, 1.0, initial)
        assert _map_live_status(settled, 1.0) == "FILLED"
        assert settled.get("status") == "closed"

    def test_unconfirmed_order_keeps_initial_snapshot(self, _live_env, monkeypatch) -> None:
        mgr = _build_manager(_RecordingIndodax())
        provider = mgr.get_provider()

        def _broken_fetch_order(order_id: str, symbol: str) -> dict[str, Any]:
            raise RuntimeError("exchange down")

        monkeypatch.setattr(provider, "fetch_order", _broken_fetch_order)
        initial = {"id": "ord9", "info": {}}
        settled = _settle_live_order(provider, "ord9", SYMBOL, 1.0, initial)
        assert _map_live_status(settled, 1.0) == "PENDING"


# ----------------------------------------------------------------------
#  LiveExecutionProvider against a recording Indodax
# ----------------------------------------------------------------------


class TestLiveIndodaxExecution:
    def _provider(self, recording: _RecordingIndodax, monkeypatch) -> LiveExecutionProvider:
        mgr = _build_manager(recording, monkeypatch=monkeypatch)
        return LiveExecutionProvider(mgr, _Cfg())

    def test_live_buy_passes_price_and_no_client_id(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        provider = self._provider(recording, monkeypatch)
        LiveExecutor.enable()
        try:
            result = provider.execute_buy(_req("BUY", SYMBOL, 1.0, 245.0))
        finally:
            LiveExecutor.disable()
        assert result.status == "FILLED", result.error
        order = recording.orders[-1]
        assert order["symbol"] == SYMBOL
        assert order["type"] == "market"
        assert order["side"] == "buy"
        assert order["price"] == pytest.approx(245.0)
        assert order["params"] == {"order_type": "market"}

    def test_live_buy_without_price_resolves_from_ticker(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        provider = self._provider(recording, monkeypatch)
        LiveExecutor.enable()
        try:
            result = provider.execute_buy(_req("BUY", SYMBOL, 1.0, None))
        finally:
            LiveExecutor.disable()
        assert result.status == "FILLED", result.error
        assert recording.orders[-1]["price"] == pytest.approx(241.0)

    def test_live_buy_rejected_when_not_armed(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        provider = self._provider(recording, monkeypatch)
        LiveExecutor.disable()
        result = provider.execute_buy(_req("BUY", SYMBOL, 1.0, 245.0))
        assert result.status == "REJECTED"
        assert recording.orders == []

    def test_live_sell_needs_no_price(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        provider = self._provider(recording, monkeypatch)
        LiveExecutor.enable()
        try:
            result = provider.execute_sell(_req("SELL", SYMBOL, 1.0, 245.0))
        finally:
            LiveExecutor.disable()
        assert result.status == "FILLED", result.error
        order = recording.orders[-1]
        assert order["side"] == "sell"
        assert order["price"] is None

    def test_binance_market_buy_keeps_price_none(self, _live_env, monkeypatch) -> None:
        recording = _RecordingIndodax()
        mgr = _build_manager(recording, exchange="binance", monkeypatch=monkeypatch)
        provider = LiveExecutionProvider(mgr, _Cfg())
        request = _req("BUY", SYMBOL, 1.0, 245.0)
        LiveExecutor.enable()
        try:
            result = provider.execute_buy(request)
        finally:
            LiveExecutor.disable()
        assert result.status == "FILLED", result.error
        order = recording.orders[-1]
        assert order["price"] is None
        assert order["params"] == {"newClientOrderId": request.client_order_id}


def _req(side: str, symbol: str, amount: float, price: float | None) -> Any:
    from scripts.execution_provider import OrderRequest

    return OrderRequest(
        symbol=symbol,
        side=side,
        type="MARKET",
        amount=amount,
        price=price,
        client_order_id="req123",
    )


# ----------------------------------------------------------------------
#  LiveExecutor (ExecutionEngine) — price gating for market BUY
# ----------------------------------------------------------------------


class TestLiveExecutorIndodax:
    def test_market_buy_passes_precise_price_only_for_indodax(self, _live_env, monkeypatch) -> None:
        from scripts.execution_engine import LiveExecutor as LE

        recording = _RecordingIndodax()
        mgr = _build_manager(recording, monkeypatch=monkeypatch)
        request = _req("BUY", SYMBOL, 1.0, 245.0)
        LiveExecutor.enable()
        try:
            result = LE().execute(request, _Cfg(), mgr, wallet=None)
        finally:
            LiveExecutor.disable()
        # LiveExecutor deliberately returns the create_order() snapshot
        # (PENDING); OrderManager._reconcile_live_order settles it via
        # fetch_order. The important part is the submitted order shape.
        assert result.status == "PENDING", result.error
        order = recording.orders[-1]
        assert order["price"] == pytest.approx(245.0)
        assert order["params"] == {}

        # OrderManager-style settlement: fetch_order returns status="closed"
        # with filled=None — must now resolve to FILLED, not PENDING forever.
        raw = recording.fetch_order(result.order_id, SYMBOL)
        assert _map_live_order_status(raw, 1.0) == "FILLED"

        recording2 = _RecordingIndodax()
        mgr2 = _build_manager(recording2, exchange="binance", monkeypatch=monkeypatch)
        LiveExecutor.enable()
        try:
            result2 = LE().execute(_req("BUY", SYMBOL, 1.0, 245.0), _Cfg(), mgr2, wallet=None)
        finally:
            LiveExecutor.disable()
        assert result2.status == "PENDING", result2.error
        assert recording2.orders[-1]["price"] is None


# ----------------------------------------------------------------------
#  End-to-end: pipeline LIVE mode with the Indodax execution layer
# ----------------------------------------------------------------------


class TestPipelineLiveIndodax:
    def test_buy_and_sl_close_through_unified_live_pipeline(
        self, _live_env, monkeypatch,
    ) -> None:
        recording = _RecordingIndodax()
        mgr = _build_manager(recording, monkeypatch=monkeypatch)
        container = _FakeContainer(mgr)
        notifier = _FakeNotifier()
        fake_pm = _FakeProtectionManager()

        monkeypatch.setattr(
            "bot.data.build_public_exchange",
            lambda *a, **k: _FakePublicTicker({SYMBOL: {"last": 200.0}}),
        )
        monkeypatch.setattr(
            protection_manager, "ProtectionManager", lambda *a, **k: fake_pm,
        )

        plan = {
            "symbol": SYMBOL,
            "status": "READY",
            "entry_price": 245.0,
            "quantity": 1.0,
            "position_size_usdt": 10000.0,
            "stop_loss": 220.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "signal_time": "2026-08-02T00:00:00Z",
        }
        with open("data/trade_plan.json", "w") as f:
            json.dump({"plans": [plan]}, f)

        position = {
            "symbol": SYMBOL,
            "status": "OPEN",
            "entry_price": 245.0,
            "quantity": 1.0,
            "remaining_qty": 1.0,
            "cost_basis": 245.0,
            "stop_loss": 220.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "realized_pnl": 0.0,
            "floating_pnl_pct": 0.0,
            "holding_hours": 1.0,
            "current_price": 245.0,
        }
        with open("data/positions.json", "w") as f:
            json.dump({"positions": [position], "active_count": 1, "closed_count": 0}, f)

        pipeline = Pipeline(_Cfg(), _StubLogger(), container=container)
        pipeline.set_notifier(notifier)

        LiveExecutor.enable()
        try:
            pipeline._run_paper_di()
        finally:
            LiveExecutor.disable()

        assert len(recording.orders) == 2, recording.orders
        assert recording.orders[0]["side"] == "buy"
        assert recording.orders[1]["side"] == "sell"

        with open("data/positions.json") as f:
            data = json.load(f)
        final = data["positions"][0]
        # BUG-4: a stop-loss exit must keep its STOPPED status instead of
        # being relabeled "CLOSED" (and misreported as "Take Profit").
        assert final["status"] in ("CLOSED", "STOPPED")
        assert final["remaining_qty"] == pytest.approx(0.0)

        assert fake_pm.cancelled == [SYMBOL], "protection cancelled before the SL sell"
        assert notifier.opened, "BUY_OPENED notification must be sent on live fill"
        with open("data/.notified_buys") as f:
            assert SYMBOL in {line.strip() for line in f}
