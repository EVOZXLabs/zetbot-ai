"""Regression tests: manual /buy must durably OPEN the paper position.

Mirror image of ``test_manual_sell_persistence.py``. The manual SELL
persistence bug was fixed by ``_close_paper_position_on_sell``; the BUY
side never got the same treatment, so a manual ``/buy`` in PAPER mode:

    * replies "✅ Buy Success" (PaperExecutor returns FILLED),
    * appends the order to ``paper_orders.json``,
    * debits cash from ``paper_balance.json``,

but NEVER creates the position in ``positions.json`` (what /positions
reads) or in ``paper_state.json`` (the authoritative state the pipeline
re-derives everything from). Net effect:

    1. /positions never shows the symbol,
    2. equity drops by the full cost (cash left, no asset booked),
    3. the next pipeline cycle reverts the cash debit entirely,
    4. the MAX_POSITIONS safety gate never counts manual buys.

The ``xfail(strict=True)`` tests below encode the CORRECT behaviour.
They are expected failures until the fix lands — and will turn the suite
RED (XPASS) the moment it does, forcing the marker to be removed.

All file access is redirected to per-test temp dirs — the bot's live
``data/`` files are never touched.
"""

import json
import os
from typing import Any

import pytest

from scripts.execution_engine import OrderRequest


BUG = (
    "BUG: scripts.order_manager._sync_paper_files() has no BUY counterpart "
    "to _close_paper_position_on_sell() — it only writes paper_orders.json "
    "and paper_balance.json, never positions.json / paper_state.json."
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every data-path constant used by the code under test."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    monkeypatch.setenv("ACCOUNT_BALANCE", "300000")
    monkeypatch.setenv("MAX_POSITIONS", "3")


# ---------------------------------------------------------------------------
#  Fixtures — production state at the moment of the incident
# ---------------------------------------------------------------------------


@pytest.fixture
def existing_position_state() -> dict[str, Any]:
    """paper_state.json holding ONE unrelated OPEN position (DODO/IDR)."""
    return {
        "version": 1,
        "balance": 284980.49990572134,
        "initial_balance": 300000.0,
        "margin_used": 0.0,
        "orders": [],
        "positions": {
            "DODO/IDR": {
                "symbol": "DODO/IDR",
                "order_id": "po_d3f8c9124fae",
                "quantity": 38.5604,
                "remaining_qty": 38.5604,
                "entry_price": 389.1167,
                "current_price": 389.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "total_pnl": 0.0,
                "cost_basis": 15019.50009427868,
                "status": "OPEN",
                "tp1": 406.18418394,
                "tp2": 423.36836789,
                "tp3": 440.55255183,
                "stop_loss": 371.81581606,
                "opened_at": "2026-08-11T04:20:01.906646+00:00",
                "closure_notified": False,
            }
        },
        "equity_history": [],
    }


@pytest.fixture
def existing_positions_json() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "DODO/IDR",
            "entry_price": 389.0,
            "current_price": 389.0,
            "quantity": 38.5604,
            "remaining_qty": 38.5604,
            "cost_basis": 15019.50009427868,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "total_pnl": 0.0,
            "status": "OPEN",
            "entry_time": "2026-08-11T04:20:01.628083+00:00",
        }
    ]


@pytest.fixture
def existing_paper_balance() -> dict[str, Any]:
    return {
        "initial_balance": 300000.0,
        "final_balance": 284980.5,
        "final_equity": 299980.5,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "net_pnl": -19.5,
        "total_return_pct": -0.01,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "gross_profit": 0,
        "gross_loss": 0,
    }


def _write(name: str, blob: Any) -> None:
    with open(f"data/{name}", "w") as f:
        json.dump(blob, f, indent=2)


def _read(name: str) -> Any:
    with open(f"data/{name}") as f:
        return json.load(f)


def _seed(state, positions, balance) -> None:
    _write("paper_state.json", state)
    _write("positions.json", {"positions": positions})
    _write("paper_balance.json", balance)
    _write("paper_orders.json", {"orders": []})


# The manual BUY as it actually happened: /buy bico 50000 @ 891.0
BICO = "BICO/IDR"
BICO_QTY = 56.11672278338945
BICO_FILL = 891.2673
BICO_COST = 50065.015


# ---------------------------------------------------------------------------
#  scripts.order_manager._sync_paper_files — the manual BUY sync helper
# ---------------------------------------------------------------------------


class TestManualBuySyncPaperFiles:
    """``_sync_paper_files(..., "BUY", ...)`` must open the position."""

    def test_buy_appends_order_and_debits_cash(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """Characterization: the two things the BUY path DOES do today."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)

        orders = _read("paper_orders.json")["orders"]
        assert len(orders) == 1
        assert orders[0]["symbol"] == BICO
        assert orders[0]["side"] == "BUY"
        assert orders[0]["status"] == "FILLED"
        assert orders[0]["cost"] == pytest.approx(BICO_COST, abs=0.01)

        pb = _read("paper_balance.json")
        assert pb["final_balance"] == pytest.approx(284980.5 - BICO_COST, abs=0.01)

    def test_buy_leaves_existing_position_untouched(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """The pre-existing DODO/IDR position must never be disturbed."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)

        dodo = {p["symbol"]: p for p in _read("positions.json")["positions"]}
        assert dodo["DODO/IDR"]["status"] == "OPEN"
        assert dodo["DODO/IDR"]["remaining_qty"] == pytest.approx(38.5604)
        state_dodo = _read("paper_state.json")["positions"]["DODO/IDR"]
        assert state_dodo["status"] == "OPEN"
    def test_buy_creates_position_in_positions_json(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """/positions reads positions.json — the new symbol must appear."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)

        data = _read("positions.json")
        by_symbol = {p["symbol"]: p for p in data["positions"]}
        assert BICO in by_symbol, "manual BUY did not appear in positions.json"

        pos = by_symbol[BICO]
        assert pos["status"] == "OPEN"
        assert pos["quantity"] == pytest.approx(BICO_QTY, abs=1e-6)
        assert pos["remaining_qty"] == pytest.approx(BICO_QTY, abs=1e-6)
        assert pos["entry_price"] == pytest.approx(BICO_FILL, abs=0.01)
        assert pos["cost_basis"] == pytest.approx(BICO_COST, abs=0.01)

        # Counters must include both positions.
        assert data["active_count"] == 2
    def test_buy_creates_position_in_paper_state_json(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """paper_state.json is authoritative — without an entry here the
        next pipeline cycle silently erases the whole trade."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)

        state = _read("paper_state.json")
        assert BICO in state["positions"], (
            "manual BUY did not appear in paper_state.json"
        )
        vp = state["positions"][BICO]
        assert vp["status"] == "OPEN"
        assert vp["remaining_qty"] == pytest.approx(BICO_QTY, abs=1e-6)
        assert vp["cost_basis"] == pytest.approx(BICO_COST, abs=0.01)

        # The DODO position must survive alongside it.
        assert state["positions"]["DODO/IDR"]["status"] == "OPEN"
    def test_buy_debits_paper_state_wallet_balance(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """The SELL path credits ``state["balance"]``; BUY must debit it,
        otherwise the cash movement is reverted next cycle."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)

        state = _read("paper_state.json")
        assert state["balance"] == pytest.approx(
            284980.49990572134 - BICO_COST, abs=0.01
        )
    def test_buy_keeps_equity_stable(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """Buying converts cash into an asset of equal value — equity must
        NOT drop by the cost of the order (observed: -50,084 IDR)."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.order_manager import _sync_paper_files

        equity_before = _read("paper_balance.json")["final_equity"]
        _sync_paper_files(BICO, "BUY", BICO_QTY, BICO_FILL, BICO_COST, 0.0)
        equity_after = _read("paper_balance.json")["final_equity"]

        # Only fees/slippage may be lost, never the whole notional.
        assert equity_after == pytest.approx(equity_before, rel=0.01), (
            f"equity fell {equity_before - equity_after:.2f} after a BUY — "
            "cash was debited but no position asset was booked"
        )


# ---------------------------------------------------------------------------
#  Auto (pipeline) BUY — the reference implementation that DOES persist
# ---------------------------------------------------------------------------


class TestAutoBuyPersistsForContrast:
    """The automatic pipeline BUY goes through a completely DIFFERENT
    executor (``execution_provider.PaperExecutionProvider``) which does
    persist. Pinning it here proves the manual path is the odd one out
    and guards the reference behaviour against regression.
    """

    def test_provider_buy_writes_position_to_paper_state(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        import scripts.execution_provider as ep

        monkeypatch.setattr(
            ep, "PAPER_STATE_PATH", os.path.abspath("data/paper_state.json"))
        monkeypatch.setattr(
            ep, "PAPER_BALANCE_PATH", os.path.abspath("data/paper_balance.json"))

        provider = ep.PaperExecutionProvider()
        provider.balance.load()

        result = provider.execute_buy(OrderRequest(
            symbol=BICO, side="BUY", type="MARKET",
            amount=BICO_QTY, price=891.0,
        ))
        assert result.status == "FILLED"

        state = _read("paper_state.json")
        assert BICO in state["positions"]
        assert state["positions"][BICO]["status"] == "OPEN"
        assert state["positions"]["DODO/IDR"]["status"] == "OPEN"

    def test_engine_paper_executor_persists_nothing(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """``execution_engine.PaperExecutor`` (the MANUAL path) is a pure
        function — it reports FILLED without touching any state file.
        That is precisely why the caller must persist, and why the
        missing BUY sync loses the position.
        """
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.execution_engine import PaperExecutor

        class _Exchange:
            name = "indodax"

            def get_ticker(self, symbol):
                return {"last": 891.0, "ask": 891.0, "bid": 890.0}

        class _Wallet:
            free_balance = 284980.5

        before_state = _read("paper_state.json")
        before_positions = _read("positions.json")

        result = PaperExecutor().execute(
            OrderRequest(symbol=BICO, side="BUY", type="MARKET",
                         amount=BICO_QTY, price=891.0),
            config=None, exchange=_Exchange(), wallet=_Wallet(),
        )

        assert result.status == "FILLED"
        assert _read("paper_state.json") == before_state
        assert _read("positions.json") == before_positions


# ---------------------------------------------------------------------------
#  End-to-end: the exact reported incident
# ---------------------------------------------------------------------------


class _StubExchange:
    name = "indodax"

    def get_ticker(self, symbol: str) -> dict[str, float]:
        return {"last": 891.0, "ask": 891.0, "bid": 890.0}


class _StubWallet:
    free_balance = 284980.5


class _StubConfig:
    quote_currency = "IDR"
    data_dir = "data"
    account_balance = 300000.0


class _StubRisk:
    def get_approved(self) -> list[dict[str, Any]]:
        return []


def _run_manual_buy(symbol: str = BICO, safeguard: Any = None) -> Any:
    """Replicate telegram/commands/buy.py end to end."""
    from scripts.order_manager import OrderManager

    om = OrderManager(_StubConfig(), _StubExchange(), _StubWallet(),
                      _StubRisk(), mode="PAPER", safeguard=safeguard)

    price = _StubExchange().get_ticker(symbol)["last"]
    request = OrderRequest(
        symbol=symbol, side="BUY", type="MARKET",
        amount=50000.0 / price, price=price,
        metadata={"source": "telegram", "bypass_risk": True},
    )
    result = om.execute(request)

    from telegram.commands._order_status import format_order_outcome
    message, should_sync = format_order_outcome("Buy", symbol, result)
    if should_sync:
        om.sync_position(result)
    return result, message, should_sync


class TestManualBuyEndToEnd:
    """existing DODO/IDR position + `/buy bico 50000` in PAPER mode."""

    def test_reply_claims_success(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        result, message, should_sync = _run_manual_buy()

        assert result.status == "FILLED"
        assert should_sync is True
        assert "Buy Success" in message
        assert "BICO/IDR" in message
        assert "paper" in message
    def test_positions_command_shows_both_symbols(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """The core user-visible symptom: /positions shows only DODO."""
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        _run_manual_buy()

        from scripts.metrics_manager import MetricsManager

        symbols = {p["symbol"] for p in MetricsManager("data").open_positions()}
        assert symbols == {"DODO/IDR", BICO}
    def test_buy_survives_next_pipeline_cycle(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pipeline re-derives paper_balance.json from the provider's
        paper_state.json. Because the manual BUY never reached
        paper_state.json, the cash debit is silently rolled back — the
        50,065 IDR reappears and the order in paper_orders.json becomes
        an orphan with no position and no cash movement behind it.
        """
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        _run_manual_buy()

        cash_after_buy = _read("paper_balance.json")["final_balance"]

        import scripts.execution_provider as ep
        monkeypatch.setattr(
            ep, "PAPER_STATE_PATH", os.path.abspath("data/paper_state.json"))
        monkeypatch.setattr(
            ep, "PAPER_BALANCE_PATH", os.path.abspath("data/paper_balance.json"))

        provider = ep.PaperExecutionProvider()
        provider.balance.load()

        from scripts.pipeline import Pipeline
        pl = Pipeline.__new__(Pipeline)
        pl.config = _StubConfig()
        Pipeline._persist_paper_state(pl, provider)

        cash_after_cycle = _read("paper_balance.json")["final_balance"]
        assert cash_after_cycle == pytest.approx(cash_after_buy, abs=1.0), (
            "pipeline reverted the manual BUY's cash debit "
            f"({cash_after_buy:.2f} -> {cash_after_cycle:.2f})"
        )
    def test_manual_buys_count_toward_max_positions(
        self, existing_position_state, existing_positions_json,
        existing_paper_balance,
    ) -> None:
        """MAX_POSITIONS is enforced by counting positions.json. Before the
        fix a manual BUY never landed there, so manual buys were invisible
        to the gate — the user could /buy without limit. Now each manual
        BUY opens a position in positions.json, so with MAX_POSITIONS=3 and
        an initial DODO/IDR position, two distinct manual buys (BICO, ADA)
        reach the ceiling and the third is refused.
        """
        _seed(existing_position_state, existing_positions_json,
              existing_paper_balance)
        from scripts.safety_limits import SafeGuard

        safeguard = SafeGuard(max_open_positions=3)
        assert safeguard.can_open_new_position()[0] is True  # 1 open < 3

        _run_manual_buy("BICO/IDR")   # 2nd open
        _run_manual_buy("ADA/IDR")    # 3rd open -> limit reached

        allowed, reason = safeguard.can_open_new_position()
        assert allowed is False, (
            "MAX_POSITIONS gate never saw the manual buys — "
            f"reason={reason!r}"
        )

