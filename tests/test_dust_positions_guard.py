"""Regression tests: exchange dust / entry-price-None live positions must
never block the LIVE bot.

Root cause: ``data/live_positions.json`` is populated from the real
exchange balance — including legacy / manual / dust holdings (VRA, RFC,
JELLYJELLY, PLPA on Indodax) whose entry price could not be reconstructed
from ``fetch_my_trades()`` history (``entry_price=None``). Every
MAX_POSITIONS-style gate was counting ALL of them, so ``open_count >=
MAX_POSITIONS`` permanently blocked every new LIVE BUY.

Covers:
  * SafeGuard max-open-positions: entry-unknown positions don't consume
    the budget, valid ones still do (dict + list file shapes)
  * risk_manager / trade_executor ``_count_open_positions`` skip
    entry-unknown live positions
  * ``bot_managed_live_positions`` helper semantics
  * /positions renders a distinguishing label for entry-unknown positions
  * maintenance / suspended markets never receive orders, and rejection
    errors abort the retry loop instead of looping pointlessly
  * ``exit_gate.reconcile_exit`` never acts on entry-unknown positions
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
#  bot_managed_live_positions / count_live_open_positions helper
# ---------------------------------------------------------------------------


class TestLivePositionHelper:
    def test_dict_shape_counts_only_entry_known(self, tmp_path, monkeypatch) -> None:
        from scripts import live_position_sync as lps

        live = tmp_path / "live_positions.json"
        live.write_text(json.dumps({
            "VRA/IDR": {"symbol": "VRA/IDR", "quantity": 999.0, "entry_price": None},
            "BTC/USDT": {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0},
            "ETH/USDT": {"symbol": "ETH/USDT", "quantity": 1.0, "entry_price": 3000.0},
        }))
        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(live))

        managed = lps.bot_managed_live_positions()
        symbols = sorted(p["symbol"] for p in managed)
        assert symbols == ["BTC/USDT", "ETH/USDT"]
        assert lps.count_live_open_positions() == 2

    def test_list_shape_counts_only_entry_known(self, tmp_path, monkeypatch) -> None:
        from scripts import live_position_sync as lps

        live = tmp_path / "live_positions.json"
        live.write_text(json.dumps({"positions": [
            {"symbol": "VRA/IDR", "quantity": 999.0, "entry_price": None},
            {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0},
        ]}))
        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(live))

        assert lps.count_live_open_positions() == 1

    def test_missing_file_counts_zero(self, tmp_path, monkeypatch) -> None:
        from scripts import live_position_sync as lps

        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(tmp_path / "nope.json"))
        assert lps.count_live_open_positions() == 0


# ---------------------------------------------------------------------------
#  risk_manager / trade_executor counters skip entry-unknown live positions
# ---------------------------------------------------------------------------


class TestRiskTradeCounters:
    def test_risk_manager_counts_only_entry_known(self, tmp_path, monkeypatch) -> None:
        from scripts import risk_manager as rm
        from scripts import live_position_sync as lps

        paper = tmp_path / "paper_state.json"
        paper.write_text(json.dumps({"positions": {}}))
        live = tmp_path / "live_positions.json"
        live.write_text(json.dumps({
            "VRA/IDR": {"symbol": "VRA/IDR", "quantity": 999.0, "entry_price": None},
            "BTC/USDT": {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0},
            "ETH/USDT": {"symbol": "ETH/USDT", "quantity": 1.0, "entry_price": 3000.0},
        }))
        monkeypatch.setattr(rm, "PAPER_STATE_PATH", str(paper))
        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(live))

        assert rm._count_open_positions() == 2

    def test_trade_executor_counts_only_entry_known(self, tmp_path, monkeypatch) -> None:
        from scripts import trade_executor as te
        from scripts import live_position_sync as lps

        paper = tmp_path / "paper_state.json"
        paper.write_text(json.dumps({"positions": {}}))
        live = tmp_path / "live_positions.json"
        live.write_text(json.dumps({
            "VRA/IDR": {"symbol": "VRA/IDR", "quantity": 999.0, "entry_price": None},
            "BTC/USDT": {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0},
        }))
        monkeypatch.setattr(te, "PAPER_STATE_PATH", str(paper))
        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(live))

        assert te._count_open_positions() == 1

    def test_paper_open_positions_still_count(self, tmp_path, monkeypatch) -> None:
        """PAPER-mode counting must be untouched — open paper positions
        still consume the budget."""
        from scripts import risk_manager as rm
        from scripts import live_position_sync as lps

        paper = tmp_path / "paper_state.json"
        paper.write_text(json.dumps({"positions": {
            "DOGE/USDT": {"status": "OPEN"},
            "XRP/USDT": {"status": "OPEN"},
        }}))
        live = tmp_path / "live_positions.json"
        live.write_text(json.dumps({"VRA/IDR": {"symbol": "VRA/IDR", "entry_price": None}}))
        monkeypatch.setattr(rm, "PAPER_STATE_PATH", str(paper))
        monkeypatch.setattr(lps, "LIVE_POSITIONS_PATH", str(live))

        assert rm._count_open_positions() == 2


# ---------------------------------------------------------------------------
#  /positions — distinguishing label for entry-unknown positions
# ---------------------------------------------------------------------------


class TestPositionsLabel:
    def _ctx(self) -> Any:
        svc = SimpleNamespace(
            exchange=MagicMock(),
            config=SimpleNamespace(quote_currency="IDR"),
        )
        return SimpleNamespace(
            services=svc,
            config=SimpleNamespace(quote_currency="IDR"),
            read_json=lambda _: {},
        )

    def test_entry_unknown_gets_exchange_balance_label(self, monkeypatch) -> None:
        from scripts import live_position_sync as lps
        from telegram.commands.positions import PositionsCommand

        fresh = [
            {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0,
             "current_price": 51000.0, "pnl_pct": 2.0, "exchange": "indodax"},
            {"symbol": "VRA/IDR", "quantity": 999.0, "entry_price": None,
             "current_price": 1.0, "pnl_pct": None, "exchange": "indodax"},
        ]
        monkeypatch.setattr(lps.LivePositionSync, "sync_all_positions",
                            lambda self: fresh)
        monkeypatch.setattr(lps, "merge_live_positions", lambda *a, **k: {})

        msg = PositionsCommand()._execute_live(self._ctx())

        assert "*BTC/USDT*" in msg
        assert "VRA/IDR" in msg
        assert "exchange balance" in msg
        assert "not bot-managed" in msg

    def test_managed_positions_have_no_label(self, monkeypatch) -> None:
        from scripts import live_position_sync as lps
        from telegram.commands.positions import PositionsCommand

        fresh = [
            {"symbol": "BTC/USDT", "quantity": 0.1, "entry_price": 50000.0,
             "current_price": 51000.0, "pnl_pct": 2.0, "exchange": "indodax"},
        ]
        monkeypatch.setattr(lps.LivePositionSync, "sync_all_positions",
                            lambda self: fresh)
        monkeypatch.setattr(lps, "merge_live_positions", lambda *a, **k: {})

        msg = PositionsCommand()._execute_live(self._ctx())

        assert "*BTC/USDT*" in msg
        assert "not bot-managed" not in msg


# ---------------------------------------------------------------------------
#  Maintenance / suspended markets never receive orders
# ---------------------------------------------------------------------------


class TestMarketTradeable:
    def test_inactive_market_is_not_tradeable(self) -> None:
        from scripts.exchange_providers import is_market_tradeable

        provider = MagicMock()
        provider.load_markets.return_value = {
            "VRA/IDR": {"active": False},
            "BTC/IDR": {"active": True},
        }
        assert is_market_tradeable(provider, "VRA/IDR") is False
        assert is_market_tradeable(provider, "BTC/IDR") is True

    def test_unknown_status_defaults_open(self) -> None:
        from scripts.exchange_providers import is_market_tradeable

        provider = MagicMock()
        provider.load_markets.return_value = {"BTC/IDR": {"active": None}}
        assert is_market_tradeable(provider, "BTC/IDR") is True

    def test_unlisted_symbol_is_not_tradeable(self) -> None:
        from scripts.exchange_providers import is_market_tradeable

        provider = MagicMock()
        provider.load_markets.return_value = {"BTC/IDR": {"active": True}}
        assert is_market_tradeable(provider, "NOPE/IDR") is False

    def test_load_failure_defaults_open(self) -> None:
        from scripts.exchange_providers import is_market_tradeable

        provider = MagicMock()
        provider.load_markets.side_effect = RuntimeError("network down")
        assert is_market_tradeable(provider, "VRA/IDR") is True

    def test_maintenance_phrase_detection(self) -> None:
        from scripts.exchange_providers import looks_like_maintenance_error

        assert looks_like_maintenance_error("VRA/IDR is under maintenance")
        assert looks_like_maintenance_error("pair suspended for trading")
        assert not looks_like_maintenance_error("connection timeout, retry")


class TestLiveProviderMaintenanceGuard:
    def test_live_buy_rejected_for_maintenance_symbol(self) -> None:
        from scripts.execution_engine import LiveExecutor
        from scripts.execution_provider import LiveExecutionProvider, OrderRequest

        class _Mgr:
            name = "indodax"

            def get_provider(self) -> Any:
                provider = MagicMock()
                provider.load_markets.return_value = {
                    "VRA/IDR": {"active": False},
                }
                return provider

        LiveExecutor.enable()
        try:
            provider = LiveExecutionProvider(_Mgr(), MagicMock())
            req = OrderRequest(
                symbol="VRA/IDR", side="BUY", type="MARKET",
                amount=100.0, price=2.0,
            )
            result = provider.execute_buy(req)
            assert result.status == "REJECTED"
            assert "maintenance" in result.error or "suspended" in result.error
        finally:
            LiveExecutor.disable()

    def test_live_sell_rejected_for_maintenance_symbol(self) -> None:
        from scripts.execution_engine import LiveExecutor
        from scripts.execution_provider import LiveExecutionProvider, OrderRequest

        class _Mgr:
            name = "indodax"

            def get_provider(self) -> Any:
                provider = MagicMock()
                provider.load_markets.return_value = {
                    "VRA/IDR": {"active": False},
                }
                return provider

        LiveExecutor.enable()
        try:
            provider = LiveExecutionProvider(_Mgr(), MagicMock())
            req = OrderRequest(
                symbol="VRA/IDR", side="SELL", type="MARKET",
                amount=100.0, price=2.0,
            )
            result = provider.execute_sell(req)
            assert result.status == "REJECTED"
            assert "maintenance" in result.error or "suspended" in result.error
        finally:
            LiveExecutor.disable()


class TestRetryAbortsOnMaintenance:
    def test_maintenance_failure_is_not_retried(self) -> None:
        from scripts.execution_engine import OrderRequest, OrderResult
        from scripts.order_manager import OrderManager

        class _FailEngine:
            mode = "LIVE"

            def execute(self, request: OrderRequest) -> OrderResult:
                return OrderResult(
                    order_id="x", status="FAILED",
                    symbol=request.symbol, side=request.side,
                    type=request.type, amount=request.amount,
                    error="Indodax: VRA/IDR is under maintenance — order rejected",
                    executor="live", exchange="indodax", mode="LIVE",
                )

        om = OrderManager(
            config=MagicMock(),
            exchange=SimpleNamespace(name="indodax"),
            wallet=MagicMock(), risk=MagicMock(), mode="LIVE",
        )
        om._engine = _FailEngine()
        req = OrderRequest(
            symbol="VRA/IDR", side="BUY", type="MARKET",
            amount=1.0, price=2.0,
        )

        result = om._execute_with_retry(req)

        assert result.status == "FAILED"
        assert "maintenance" in result.error
        # No retry loop ran (RETRY_MAX is 3) — maintenance is not transient.
        assert result.retries == 1


# ---------------------------------------------------------------------------
#  exit_gate never acts on entry-unknown positions (defense-in-depth)
# ---------------------------------------------------------------------------


class TestExitGateSkipsEntryUnknown:
    def test_reconcile_exit_does_not_sell_entry_unknown(self, tmp_path, monkeypatch) -> None:
        from scripts import exit_gate

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        with open("data/positions.json", "w") as f:
            json.dump({
                "positions": [{
                    "symbol": "VRA/IDR", "status": "OPEN", "entry_price": None,
                }],
            }, f)

        calls: list = []

        class _Pipe:
            def reconcile_position(self, *args, **kwargs):
                calls.append(args)
                return None

        result = exit_gate.reconcile_exit(_Pipe(), "VRA/IDR", 1.0)

        assert calls == []
        assert result is not None
        assert result["symbol"] == "VRA/IDR"

    def test_reconcile_exit_still_sells_entry_known(self, tmp_path, monkeypatch) -> None:
        from scripts import exit_gate

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        with open("data/positions.json", "w") as f:
            json.dump({
                "positions": [{
                    "symbol": "BTC/USDT", "status": "OPEN", "entry_price": 50000.0,
                }],
            }, f)

        calls: list = []

        class _Pipe:
            def reconcile_position(self, symbol, price, pos, plan):
                calls.append(symbol)
                return None

        exit_gate.reconcile_exit(_Pipe(), "BTC/USDT", 50000.0)

        assert calls == ["BTC/USDT"]
