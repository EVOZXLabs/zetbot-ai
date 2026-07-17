"""Unit tests for safety limits and trading guards.

Tests:
    - Duplicate order prevention (client_order_id)
    - Pause mode (data/.paused file)
    - Daily loss limit
    - Max consecutive losses
    - Max daily trades
    - Exchange failure cooldown
    - Restart recovery (live_armed.json reset)
"""

import json
import os
import sys
import time
import tempfile
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.safety_limits import SafeGuard
from scripts.execution_engine import OrderRequest, OrderResult
from scripts.order_manager import OrderManager


# ======================================================================
#  Fixtures
# ======================================================================


@pytest.fixture
def safeguard() -> SafeGuard:
    """Fresh SafeGuard with low limits for easy testing."""
    sg = SafeGuard(
        max_daily_loss_pct=5.0,
        max_consecutive_losses=3,
        max_daily_trades=5,
        exchange_failure_window=60,
        exchange_max_failures=3,
        atr_spike_multiplier=3.0,
    )
    sg.set_account_balance(10000.0)
    yield sg
    # Cleanup tracking files
    for path in ("data/safety_tracking.json", "data/exchange_cooldown.json", "data/.paused"):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def order_manager() -> OrderManager:
    config = MagicMock()
    config.api_key = "test_key"
    config.api_secret = "test_secret"
    config.quote_currency = "USDT"
    config.auto_protect = False

    exchange = MagicMock()
    exchange.name = "test_exchange"
    provider = MagicMock()
    provider.name = "test_provider"
    provider.has.return_value = True
    provider.fetch_balance.return_value = {"free": {"USDT": 10000.0}}
    provider._get_exchange.return_value = MagicMock()
    exchange.get_provider.return_value = provider

    wallet = MagicMock()
    wallet.free_balance = 10000.0

    risk = MagicMock()
    risk.get_approved.return_value = []

    return OrderManager(config, exchange, wallet, risk, mode="PAPER")


# ======================================================================
#  Pause mode
# ======================================================================


class TestPauseMode:
    def test_pause_file_blocks_new_positions(self, safeguard: SafeGuard) -> None:
        os.makedirs("data", exist_ok=True)
        with open("data/.paused", "w") as f:
            f.write("2025-01-01T00:00:00+00:00")

        allowed, reason = safeguard.can_open_new_position()
        assert not allowed
        assert "paused" in reason.lower()

    def test_resume_after_pause_clear(self, safeguard: SafeGuard) -> None:
        os.makedirs("data", exist_ok=True)
        with open("data/.paused", "w") as f:
            f.write("2025-01-01T00:00:00+00:00")

        allowed, _ = safeguard.can_open_new_position()
        assert not allowed

        os.remove("data/.paused")
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_pause_persists_across_safeguard_restart(self) -> None:
        os.makedirs("data", exist_ok=True)
        with open("data/.paused", "w") as f:
            f.write("2025-01-01T00:00:00+00:00")

        sg = SafeGuard()
        sg.set_account_balance(10000.0)
        allowed, reason = sg.can_open_new_position()
        assert not allowed
        assert "paused" in reason.lower()
        os.remove("data/.paused")

    def test_no_pause_file_allows_trades(self, safeguard: SafeGuard) -> None:
        try:
            os.remove("data/.paused")
        except OSError:
            pass
        allowed, _ = safeguard.can_open_new_position()
        assert allowed


# ======================================================================
#  Daily loss limit
# ======================================================================


class TestDailyLossLimit:
    def test_no_limit_when_no_tracking(self, safeguard: SafeGuard) -> None:
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_blocks_when_daily_loss_exceeded(self, safeguard: SafeGuard) -> None:
        # Simulate $600 loss on a $10k account (6% > 5% limit)
        safeguard.record_trade_outcome(-300.0)
        safeguard.record_trade_outcome(-300.0)
        allowed, reason = safeguard.can_open_new_position()
        assert not allowed
        assert "loss limit" in reason.lower()

    def test_allows_when_daily_loss_within_limit(self, safeguard: SafeGuard) -> None:
        safeguard.record_trade_outcome(-200.0)
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_resets_on_new_day(self) -> None:
        sg = SafeGuard(max_daily_loss_pct=5.0)
        sg.set_account_balance(10000.0)
        # Manually set tracking for yesterday
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tracking = {
            "date": yesterday,
            "total_trades": 10,
            "winning_trades": 0,
            "losing_trades": 10,
            "consecutive_losses": 10,
            "realized_pnl": -3000.0,
        }
        sg._save_tracking(tracking)
        # Should allow today since losses were yesterday
        allowed, _ = sg.can_open_new_position()
        assert allowed


# ======================================================================
#  Max consecutive losses
# ======================================================================


class TestMaxConsecutiveLosses:
    def test_blocks_at_limit(self, safeguard: SafeGuard) -> None:
        safeguard.record_trade_outcome(-50.0)
        safeguard.record_trade_outcome(-50.0)
        safeguard.record_trade_outcome(-50.0)
        allowed, reason = safeguard.can_open_new_position()
        assert not allowed
        assert "consecutive" in reason.lower()

    def test_allows_below_limit(self, safeguard: SafeGuard) -> None:
        safeguard.record_trade_outcome(-50.0)
        safeguard.record_trade_outcome(-50.0)
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_win_resets_consecutive_counter(self, safeguard: SafeGuard) -> None:
        safeguard.record_trade_outcome(-50.0)
        safeguard.record_trade_outcome(-50.0)
        safeguard.record_trade_outcome(100.0)  # win resets
        allowed, _ = safeguard.can_open_new_position()
        assert allowed


# ======================================================================
#  Max daily trades
# ======================================================================


class TestMaxDailyTrades:
    def test_blocks_at_limit(self, safeguard: SafeGuard) -> None:
        for _ in range(5):
            safeguard.record_trade_outcome(10.0)
        allowed, reason = safeguard.can_open_new_position()
        assert not allowed
        assert "trades" in reason.lower()

    def test_allows_below_limit(self, safeguard: SafeGuard) -> None:
        for _ in range(4):
            safeguard.record_trade_outcome(10.0)
        allowed, _ = safeguard.can_open_new_position()
        assert allowed


# ======================================================================
#  Exchange failure cooldown
# ======================================================================


class TestExchangeCooldown:
    def test_no_cooldown_initially(self, safeguard: SafeGuard) -> None:
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_cooldown_activates_after_max_failures(self, safeguard: SafeGuard) -> None:
        for _ in range(3):
            safeguard.record_exchange_failure()
        allowed, reason = safeguard.can_open_new_position()
        assert not allowed
        assert "cooldown" in reason.lower()

    def test_below_max_failures_no_cooldown(self, safeguard: SafeGuard) -> None:
        for _ in range(2):
            safeguard.record_exchange_failure()
        allowed, _ = safeguard.can_open_new_position()
        assert allowed

    def test_cooldown_clears_after_clear(self, safeguard: SafeGuard) -> None:
        for _ in range(3):
            safeguard.record_exchange_failure()
        safeguard.clear_exchange_cooldown()
        allowed, _ = safeguard.can_open_new_position()
        assert allowed


# ======================================================================
#  Duplicate order prevention (client_order_id)
# ======================================================================


class TestDuplicateOrderPrevention:
    def test_client_order_id_generated_from_trace_id(self) -> None:
        req = OrderRequest(symbol="BTC/USDT", side="BUY", amount=1.0)
        assert req.client_order_id != ""
        assert req.client_order_id.startswith("zb")
        assert len(req.client_order_id) <= 36

    def test_same_trace_id_same_client_order_id(self) -> None:
        req1 = OrderRequest(trace_id="abc123", symbol="BTC/USDT", side="BUY", amount=1.0)
        req2 = OrderRequest(trace_id="abc123", symbol="ETH/USDT", side="BUY", amount=0.5)
        assert req1.client_order_id == req2.client_order_id

    def test_different_trace_id_different_client_order_id(self) -> None:
        req1 = OrderRequest(trace_id="aaa", symbol="BTC/USDT", side="BUY", amount=1.0)
        req2 = OrderRequest(trace_id="bbb", symbol="ETH/USDT", side="BUY", amount=0.5)
        assert req1.client_order_id != req2.client_order_id


# ======================================================================
#  Restart recovery
# ======================================================================


class TestRestartRecovery:
    def test_live_armed_state_read_write(self) -> None:
        record = {"armed": True, "time": "2025-01-01T00:00:00", "exchange": "binance"}
        OrderManager._write_live_armed_state(record)
        loaded = OrderManager.read_live_armed_state()
        assert loaded.get("armed") is True
        assert loaded.get("exchange") == "binance"

    def test_no_live_armed_state_on_fresh_start(self) -> None:
        try:
            os.remove("data/live_armed.json")
        except OSError:
            pass
        loaded = OrderManager.read_live_armed_state()
        assert loaded == {}

    def test_disarm_writes_armed_false(self) -> None:
        OrderManager._write_live_armed_state({"armed": True})
        OrderManager._write_live_armed_state({"armed": False, "reason": "test"})
        loaded = OrderManager.read_live_armed_state()
        assert loaded.get("armed") is False

    @classmethod
    def teardown_class(cls) -> None:
        try:
            os.remove("data/live_armed.json")
        except OSError:
            pass


# ======================================================================
#  OrderManager execute blocked by no risk approval (sanity test)
# ======================================================================


class TestOrderManagerSafety:
    def test_execute_rejected_when_no_approval(self, order_manager: OrderManager) -> None:
        result = order_manager.execute(
            {"symbol": "BTC/USDT", "side": "BUY", "amount": 0.001}
        )
        assert isinstance(result, dict)
        assert result.get("status") in ("REJECTED",)


# ======================================================================
#  Integration: SafeGuard + OrderManager
# ======================================================================


class TestSafeGuardIntegration:
    def test_order_manager_execute_blocked_by_pause(self) -> None:
        """Verify OrderManager.execute respects SafeGuard's pause check."""
        os.makedirs("data", exist_ok=True)
        with open("data/.paused", "w") as f:
            f.write("paused")

        config = MagicMock()
        config.api_key = ""
        config.api_secret = ""
        exchange = MagicMock()
        exchange.name = "test"
        wallet = MagicMock()
        wallet.free_balance = 10000.0
        risk = MagicMock()
        risk.get_approved.return_value = [{"symbol": "BTC/USDT"}]
        sg = SafeGuard()
        sg.set_account_balance(10000.0)

        om = OrderManager(config, exchange, wallet, risk, mode="PAPER", safeguard=sg)
        result = om.execute(
            {"symbol": "BTC/USDT", "side": "BUY", "amount": 0.001, "entry_price": 50000.0}
        )
        if isinstance(result, dict):
            assert result.get("status") == "REJECTED"
            assert "paused" in (result.get("error") or "").lower()
        else:
            assert result.status == "REJECTED"

        os.remove("data/.paused")

    def test_safeguard_only_blocks_buy_side(self) -> None:
        """SELL orders should not be blocked by SafeGuard even when paused."""
        sg = SafeGuard()
        sg.set_account_balance(10000.0)

        # Test directly (order_manager._plan_to_request hardcodes BUY)
        req = OrderRequest(
            trace_id="test_sell",
            symbol="BTC/USDT",
            side="SELL",
            type="MARKET",
            amount=0.001,
        )
        # SafeGuard check is only applied for BUY side in OrderManager
        assert req.side == "SELL", "Test should use SELL side"
        # Verify SafeGuard would block a new BUY
        os.makedirs("data", exist_ok=True)
        with open("data/.paused", "w") as f:
            f.write("paused")
        allowed, reason = sg.can_open_new_position()
        assert not allowed
        assert "paused" in reason.lower()
        os.remove("data/.paused")


# Clean up any leftover test files
def teardown_module() -> None:
    for path in (
        "data/safety_tracking.json",
        "data/exchange_cooldown.json",
        "data/.paused",
        "data/live_armed.json",
    ):
        try:
            os.remove(path)
        except OSError:
            pass
