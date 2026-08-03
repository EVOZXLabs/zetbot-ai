"""Regression tests: manual /sell must durably close the paper position.

The pipeline re-derives all state from ``data/paper_state.json`` every
cycle. A manual SELL that only updated ``paper_balance.json`` /
``paper_orders.json`` was silently reverted on the next cycle — the
position reloaded as still-OPEN and the cash figure reverted to the
pre-sale balance. These tests pin the fix:

1. ``scripts.order_manager._close_paper_position_on_sell`` closes the
   position in ``paper_state.json`` AND ``positions.json``.
2. ``PaperExecutionProvider.execute_sell`` does the same and persists
   the wallet.

All file access is redirected to per-test temp dirs — the bot's live
``data/`` files are never touched.
"""

import json
import os
from typing import Any

import pytest

from scripts.execution_engine import (
    OrderRequest,
    OrderType,
    OrderSide,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect every data-path constant used by the code under test."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)


@pytest.fixture
def open_position_state() -> dict[str, Any]:
    """A paper_state.json with one OPEN position, mirroring live shape."""
    return {
        "version": 1,
        "balance": 173992.27570064322,
        "initial_balance": 180000.0,
        "margin_used": 0.0,
        "orders": [
            {
                "id": "PAPER-20260730233406-0001",
                "symbol": "ADA/IDR",
                "side": "BUY",
                "type": "MARKET",
                "quantity": 1.9614,
                "filled_quantity": 1.9614,
                "fill_price": 3063.04,
                "total_cost": 6007.72429935678,
                "status": "FILLED",
                "created_at": "2026-07-30T23:34:06",
                "filled_at": "2026-07-30T23:34:06",
            }
        ],
        "positions": {
            "ADA/IDR": {
                "symbol": "ADA/IDR",
                "order_id": "PAPER-20260730233406-0001",
                "quantity": 1.9614,
                "remaining_qty": 1.9614,
                "entry_price": 3059.9177,
                "current_price": 3066.0,
                "unrealized_pnl": 5.93,
                "realized_pnl": 0.0,
                "total_pnl": 5.93,
                "cost_basis": 6007.72429935678,
                "status": "OPEN",
                "opened_at": "2026-07-30T23:34:06",
                "closure_notified": False,
            }
        },
        "equity_history": [],
    }


def _write_state(state: dict[str, Any]) -> None:
    with open("data/paper_state.json", "w") as f:
        json.dump(state, f, indent=2)


def _write_positions(positions: list[dict[str, Any]]) -> None:
    with open("data/positions.json", "w") as f:
        json.dump({"positions": positions}, f, indent=2)


# ---------------------------------------------------------------------------
#  scripts.order_manager._close_paper_position_on_sell
# ---------------------------------------------------------------------------


class TestClosePaperPositionOnSell:
    """The /sell sync helper must durably close the state position."""

    def test_closes_position_in_paper_state(
        self, open_position_state: dict[str, Any],
    ) -> None:
        _write_state(open_position_state)
        from scripts.order_manager import _close_paper_position_on_sell

        proceeds = 1.9614 * 3065.0802  # ~6011.8 (no fee)
        pnl = _close_paper_position_on_sell("ADA/IDR", 1.9614, 3065.0802, proceeds)

        with open("data/paper_state.json") as f:
            state = json.load(f)

        # Wallet credited with the proceeds.
        assert state["balance"] == pytest.approx(
            173992.27570064322 + proceeds, abs=0.01
        )

        # Position closed — must NOT be resurrected by the next cycle.
        vp = state["positions"]["ADA/IDR"]
        assert vp["status"] == "CLOSED"
        assert vp["remaining_qty"] == 0.0
        assert vp["unrealized_pnl"] == 0.0
        assert vp["current_price"] == 3065.0802
        assert vp["closure_notified"] is True

        # Realized PnL = proceeds - (cost_basis * sold fraction).
        cost_part = open_position_state["positions"]["ADA/IDR"]["cost_basis"] * 1.0
        assert pnl == pytest.approx(proceeds - cost_part, abs=0.01)
        assert vp["realized_pnl"] == pytest.approx(pnl, abs=0.01)
        assert vp["total_pnl"] == pytest.approx(pnl, abs=0.01)

        # SELL order appended to the state.
        sell_orders = [
            o for o in state["orders"]
            if o["side"] == "SELL" and o["symbol"] == "ADA/IDR"
        ]
        assert len(sell_orders) == 1
        assert sell_orders[0]["quantity"] == 1.9614
        assert sell_orders[0]["fill_price"] == 3065.0802
        assert sell_orders[0]["status"] == "CLOSED"

    def test_updates_positions_json(
        self, open_position_state: dict[str, Any],
    ) -> None:
        _write_state(open_position_state)
        _write_positions([
            {
                "symbol": "ADA/IDR",
                "status": "OPEN",
                "remaining_qty": 1.9614,
                "unrealized_pnl": 5.93,
                "realized_pnl": 0.0,
                "total_pnl": 5.93,
            },
            {
                "symbol": "XRP/IDR",
                "status": "OPEN",
                "remaining_qty": 1000.0,
                "unrealized_pnl": 10.0,
                "realized_pnl": 0.0,
                "total_pnl": 10.0,
            },
        ])
        from scripts.order_manager import _close_paper_position_on_sell

        _close_paper_position_on_sell("ADA/IDR", 1.9614, 3065.0802, 6011.8)

        with open("data/positions.json") as f:
            data = json.load(f)

        by_symbol = {p["symbol"]: p for p in data["positions"]}
        assert by_symbol["ADA/IDR"]["status"] == "CLOSED"
        assert by_symbol["ADA/IDR"]["remaining_qty"] == 0.0
        assert by_symbol["XRP/IDR"]["status"] == "OPEN"  # untouched
        assert data["active_count"] == 1
        assert data["closed_count"] == 1

    def test_returns_zero_when_no_open_position(
        self, open_position_state: dict[str, Any],
    ) -> None:
        _write_state(open_position_state)
        from scripts.order_manager import _close_paper_position_on_sell

        # Unknown symbol → no-op, balance unchanged.
        assert _close_paper_position_on_sell("DOGE/IDR", 1.0, 100.0, 100.0) == 0.0

        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert state["balance"] == open_position_state["balance"]

    def test_sell_is_idempotent(
        self, open_position_state: dict[str, Any],
    ) -> None:
        """A second SELL for the same symbol must not double-credit."""
        _write_state(open_position_state)
        from scripts.order_manager import _close_paper_position_on_sell

        _close_paper_position_on_sell("ADA/IDR", 1.9614, 3065.0802, 6011.8)
        first_balance = json.load(open("data/paper_state.json"))["balance"]

        assert _close_paper_position_on_sell("ADA/IDR", 1.9614, 3065.0802, 6011.8) == 0.0
        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert state["balance"] == first_balance

    def test_closes_positions_json_even_when_paper_state_out_of_sync(
        self,
    ) -> None:
        """Regression: a manual /sell on a symbol that IS open in
        ``positions.json`` but has no matching OPEN entry in
        ``paper_state.json`` (e.g. state drifted, was reset, or never
        had the symbol) used to hit an early ``return 0.0`` before ever
        reaching the ``positions.json`` closure block below it — leaving
        /positions and /status showing a position that had already been
        sold. It must close in ``positions.json`` regardless.
        """
        # paper_state.json has NO entry at all for SHIB/IDR.
        _write_state({
            "version": 1,
            "balance": 1_009_974.03,
            "initial_balance": 1_000_000.0,
            "margin_used": 0.0,
            "orders": [],
            "positions": {},
            "equity_history": [],
        })
        _write_positions([
            {
                "symbol": "SHIB/IDR",
                "status": "OPEN",
                "remaining_qty": 110549.1906,
                "entry_price": 0.0913,
                "cost_basis": 10093.0,
                "unrealized_pnl": -12.99,
                "realized_pnl": 0.0,
                "total_pnl": -12.99,
            },
        ])
        from scripts.order_manager import _close_paper_position_on_sell

        proceeds = 110549.1906 * 0.090313
        pnl = _close_paper_position_on_sell(
            "SHIB/IDR", 110549.1906, 0.090313, proceeds,
        )

        with open("data/positions.json") as f:
            data = json.load(f)
        by_symbol = {p["symbol"]: p for p in data["positions"]}

        assert by_symbol["SHIB/IDR"]["status"] == "CLOSED"
        assert by_symbol["SHIB/IDR"]["remaining_qty"] == 0.0
        assert data["active_count"] == 0
        assert data["closed_count"] == 1
        # Falls back to positions.json's own cost_basis for PnL since
        # paper_state.json had nothing to compute it from.
        assert pnl == pytest.approx(proceeds - 10093.0, abs=0.01)


# ---------------------------------------------------------------------------
#  scripts.execution_provider.PaperExecutionProvider.execute_sell
# ---------------------------------------------------------------------------


class TestPaperExecutionProviderSell:
    """The provider used by the ExecutionEngine must close the position."""

    def test_execute_sell_closes_position_and_persists(
        self, open_position_state: dict[str, Any],
    ) -> None:
        _write_state(open_position_state)
        import scripts.execution_provider as ep

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            ep, "PAPER_STATE_PATH", os.path.abspath("data/paper_state.json")
        )
        monkeypatch.setattr(
            ep, "PAPER_BALANCE_PATH", os.path.abspath("data/paper_balance.json")
        )

        provider = ep.PaperExecutionProvider()
        provider.balance.load()

        request = OrderRequest(
            symbol="ADA/IDR",
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            amount=1.9614,
            price=3065.0802,
        )
        result = provider.execute_sell(request)

        assert result.status == "FILLED"
        assert result.executor == "paper"

        with open("data/paper_state.json") as f:
            state = json.load(f)
        vp = state["positions"]["ADA/IDR"]
        assert vp["status"] == "CLOSED"
        assert vp["remaining_qty"] == 0.0

        # A fresh provider reloads the persisted, credited balance.
        reloaded = ep.PaperExecutionProvider()
        reloaded.balance.load()
        assert reloaded.balance.balance == pytest.approx(
            provider.balance.balance, abs=0.01
        )

        # The SELL order was appended to the state orders list.
        sell_orders = [
            o for o in state["orders"]
            if o["side"] == "SELL" and o["symbol"] == "ADA/IDR"
        ]
        assert len(sell_orders) == 1
        assert sell_orders[0]["status"] == "CLOSED"
        assert sell_orders[0]["exit_reason"] == "market_sell"

        monkeypatch.undo()

    def test_execute_sell_writes_complete_order_record(
        self, open_position_state: dict[str, Any],
    ) -> None:
        """Regression: the provider's SELL order used to omit 7 ``Order``
        fields, so ``PaperTradingEngine._load_state`` crashed on the next
        startup (``Order.__init__() missing 7 required positional
        arguments``). The persisted record must contain every dataclass
        field so the engine can always reload it.
        """
        import dataclasses

        _write_state(open_position_state)
        import scripts.execution_provider as ep
        from scripts.paper_trading_engine import Order

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            ep, "PAPER_STATE_PATH", os.path.abspath("data/paper_state.json")
        )
        monkeypatch.setattr(
            ep, "PAPER_BALANCE_PATH", os.path.abspath("data/paper_balance.json")
        )

        provider = ep.PaperExecutionProvider()
        provider.balance.load()
        provider.execute_sell(OrderRequest(
            symbol="ADA/IDR",
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            amount=1.9614,
            price=3065.0802,
        ))

        with open("data/paper_state.json") as f:
            state = json.load(f)
        sell_orders = [
            o for o in state["orders"]
            if o["side"] == "SELL" and o["symbol"] == "ADA/IDR"
        ]
        assert len(sell_orders) == 1

        order_fields = {f.name for f in dataclasses.fields(Order)}
        missing = order_fields - set(sell_orders[0])
        assert not missing, f"SELL order missing Order fields: {missing}"

        # The coercion path must round-trip it back into an Order.
        from scripts.paper_trading_engine import ORDER_FIELD_DEFAULTS
        coerce_kwargs = {
            k: v for k, v in sell_orders[0].items() if k in ORDER_FIELD_DEFAULTS
        }
        Order(**{**ORDER_FIELD_DEFAULTS, **coerce_kwargs})

        monkeypatch.undo()

    def test_execute_sell_keeps_state_history(
        self, open_position_state: dict[str, Any],
    ) -> None:
        """Selling must preserve existing orders/equity_history in state."""
        open_position_state["equity_history"] = [
            {
                "timestamp": "2026-07-31T00:42:27",
                "balance": 173992.28,
                "equity": 180013.77,
                "realized_pnl": 0.0,
                "unrealized_pnl": 21.49,
                "free_balance": 173992.28,
            }
        ]
        _write_state(open_position_state)
        import scripts.execution_provider as ep

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            ep, "PAPER_STATE_PATH", os.path.abspath("data/paper_state.json")
        )
        monkeypatch.setattr(
            ep, "PAPER_BALANCE_PATH", os.path.abspath("data/paper_balance.json")
        )

        provider = ep.PaperExecutionProvider()
        provider.balance.load()
        provider.execute_sell(OrderRequest(
            symbol="ADA/IDR",
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            amount=1.9614,
            price=3065.0802,
        ))

        with open("data/paper_state.json") as f:
            state = json.load(f)
        assert len(state["equity_history"]) == 1
        assert len(state["orders"]) == 2  # original BUY + appended SELL
        monkeypatch.undo()
