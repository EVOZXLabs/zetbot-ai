"""Tests for currency formatting — USDT and IDR support.

Verifies:
  - fmt_balance() outputs currency code
  - fmt_pnl() outputs currency code
  - fmt_compact_number() outputs currency code
  - No bare "$" appears in Telegram command output
  - IDR pairs display IDR, USDT pairs display USDT
  - All Telegram command outputs use currency-aware formatting
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from telegram.formatter import fmt_balance, fmt_pnl, fmt_compact_number


# ---------------------------------------------------------------------------
#  fmt_balance tests
# ---------------------------------------------------------------------------

class TestFmtBalance:
    def test_default_currency(self):
        assert fmt_balance(10000.00) == "10,000.00 USDT"

    def test_explicit_usdt(self):
        assert fmt_balance(10000.00, "USDT") == "10,000.00 USDT"

    def test_explicit_idr(self):
        assert fmt_balance(15000000.00, "IDR") == "15,000,000.00 IDR"

    def test_zero(self):
        assert fmt_balance(0.0) == "0.00 USDT"

    def test_negative(self):
        assert fmt_balance(-100.50, "USDT") == "-100.50 USDT"

    def test_thousands_sep(self):
        assert fmt_balance(1234567.89, "USDT") == "1,234,567.89 USDT"


# ---------------------------------------------------------------------------
#  fmt_pnl tests
# ---------------------------------------------------------------------------

class TestFmtPnl:
    def test_default_no_currency(self):
        assert fmt_pnl(250.00) == "+250.00"

    def test_with_usdt(self):
        assert fmt_pnl(250.00, "USDT") == "+250.00 USDT"

    def test_with_idr(self):
        assert fmt_pnl(-7800.00, "IDR") == "-7,800.00 IDR"

    def test_negative_without_currency(self):
        assert fmt_pnl(-7.80) == "-7.80"

    def test_positive_with_currency(self):
        assert fmt_pnl(150.00, "USDT") == "+150.00 USDT"


# ---------------------------------------------------------------------------
#  fmt_compact_number tests
# ---------------------------------------------------------------------------

class TestFmtCompactNumber:
    def test_thousands_usdt(self):
        result = fmt_compact_number(1500.00)
        assert "K" in result and "USDT" in result

    def test_millions_usdt(self):
        result = fmt_compact_number(2_500_000.00)
        assert "M" in result and "USDT" in result

    def test_small_number(self):
        assert fmt_compact_number(500.00) == "500.00 USDT"

    def test_billions(self):
        result = fmt_compact_number(3_000_000_000.00)
        assert "B" in result and "USDT" in result


# ---------------------------------------------------------------------------
#  No bare "$" in output
# ---------------------------------------------------------------------------

class TestNoDollarSign:
    """Verify no Telegram output contains a bare '$'."""

    def test_fmt_balance_no_dollar(self):
        assert "$" not in fmt_balance(10000.00)

    def test_fmt_pnl_no_dollar(self):
        assert "$" not in fmt_pnl(250.00, "USDT")

    def test_fmt_compact_no_dollar(self):
        assert "$" not in fmt_compact_number(1500.00)

    def test_telegram_status_no_dollar(self, monkeypatch):
        """/status output must not contain $."""
        from telegram.commands.status import StatusCommand
        cmd = StatusCommand()
        ctx = _mock_ctx(quote_currency="USDT")
        result = cmd.execute(ctx, "")
        assert "$" not in result, f"Found $ in /status output: {result[:200]}"

    def test_telegram_wallet_no_dollar(self, monkeypatch):
        """/wallet output must not contain $."""
        from telegram.commands.wallet import WalletCommand
        cmd = WalletCommand()
        ctx = _mock_ctx(quote_currency="USDT")
        result = cmd.execute(ctx, "")
        assert "$" not in result, f"Found $ in /wallet output: {result[:200]}"

    def test_telegram_portfolio_no_dollar(self, monkeypatch):
        """/portfolio output must not contain $."""
        from telegram.commands.portfolio import PortfolioCommand
        cmd = PortfolioCommand()
        ctx = _mock_ctx(quote_currency="USDT")
        result = cmd.execute(ctx, "")
        assert "$" not in result, f"Found $ in /portfolio output: {result[:200]}"


# ---------------------------------------------------------------------------
#  Symbol-based quote currency detection
# ---------------------------------------------------------------------------

class TestPairCurrency:
    def test_usdt_pair(self):
        """BTC/USDT should display P&L in USDT."""
        from telegram.formatter import fmt_pnl
        pair = "BTC/USDT"
        quote = pair.split("/")[1]
        result = fmt_pnl(250.00, quote)
        assert "USDT" in result

    def test_idr_pair(self):
        """BTC/IDR should display P&L in IDR."""
        from telegram.formatter import fmt_pnl
        pair = "MUBARAK/IDR"
        quote = pair.split("/")[1]
        result = fmt_pnl(-7800.00, quote)
        assert "IDR" in result


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _mock_ctx(quote_currency: str = "USDT"):
    """Create a mock context object for testing commands."""
    ctx = MagicMock()
    ctx.services.config.quote_currency = quote_currency
    ctx.services.metrics.account.return_value = _mock_account_snapshot()
    ctx.services.metrics.open_positions.return_value = []
    ctx.services.metrics.today_summary.return_value = {"pnl": 0.0}
    health_mock = MagicMock()
    health_mock.uptime_sec = 3600
    health_mock.snapshot.return_value = {"health_score": 100}
    ctx.services.health = health_mock
    ctx.services.scheduler.status = "idle"
    ctx.services.scheduler.next_run = None
    ctx.read_json.return_value = None
    ctx.chat_id = "123"
    return ctx


def _mock_account_snapshot(
    balance: float = 10000.0,
    equity: float = 10000.0,
    realized_pnl: float = 250.0,
    unrealized_pnl: float = 0.0,
    net_pnl: float = 250.0,
    total_return_pct: float = 2.5,
    total_trades: int = 5,
    winning_trades: int = 3,
    losing_trades: int = 2,
    win_rate: float = 60.0,
    profit_factor: float = 1.5,
    gross_profit: float = 500.0,
    gross_loss: float = 250.0,
    open_positions: int = 1,
    initial_balance: float = 10000.0,
    position_value: float = 0.0,
    exposure_pct: float = 0.0,
):
    from telegram.formatter import fmt_balance
    from scripts.metrics_manager import AccountSnapshot
    return AccountSnapshot(
        balance=balance,
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
        open_positions=open_positions,
        initial_balance=initial_balance,
        position_value=position_value,
        exposure_pct=exposure_pct,
    )
