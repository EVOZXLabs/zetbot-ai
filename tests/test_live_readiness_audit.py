"""
Regression tests for the final LIVE-TRADING READINESS audit fixes.

Covers:
  * token redaction in Telegram logs (no ``bot<token>`` ever lands in logs)
  * additive chat authorization (Telegram + WhatsApp coexistence)
  * reply chunking (Telegram's 4096-char limit)
  * SafeGuard max-open-positions counting live_positions.json in LIVE mode
  * OrderManager retry abort on exchanges that cannot tag orders (indodax)
  * TIMEFRAME propagation to the scanner config
  * /buy per-symbol single-position guard (paper + live)
  * /sell partial amount + live-position resolution
  * /restart /reload /stoploss /takeprofit no longer placeholders
"""

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
#  Telegram token hygiene
# ---------------------------------------------------------------------------


class TestTokenRedaction:
    """request URLs embedding ``/bot<token>/`` must never reach logs."""

    def test_redact_strips_token_from_exception_text(self) -> None:
        from scripts import telegram_commands as tc

        raw = (
            "HTTPSConnectionPool: POST https://api.telegram.org/"
            "bot123456789:AAfaketokenREDACTEDtestvalue123456/sendMessage "
            "timed out"
        )
        redacted = tc._redact(raw)
        assert "AAfaketokenREDACTEDtestvalue123456" not in redacted
        assert "bot<REDACTED>" in redacted

    def test_log_sink_never_emits_token(self, capsys: pytest.CaptureFixture) -> None:
        from scripts import telegram_commands as tc

        tc._log("failed: " + "api.telegram.org/"
                "bot123456789:AAfaketokenREDACTEDtestvalue123456/foo")
        out = capsys.readouterr().out
        assert "AAfaketokenREDACTEDtestvalue123456" not in out
        assert "bot<REDACTED>" in out

    def test_bot_telegram_logger_redacts(self) -> None:
        from bot.telegram import _redact

        text = _redact("url api.telegram.org/bot12345:AAHsecr3tValueXYZ123/")
        assert "AAHsecr3tValueXYZ123" not in text
        assert "bot<REDACTED>" in text


# ---------------------------------------------------------------------------
#  Authorization (additive, multi-channel)
# ---------------------------------------------------------------------------


class TestPermissionsAdditive:
    def test_second_channel_does_not_revoke_first(self) -> None:
        from telegram.permissions import configure, ALLOWED_CHAT_IDS

        configure("111")
        configure("222")
        try:
            # Other test files may register ids too — what matters is that
            # BOTH channel ids survive (no last-writer-wins clearing).
            assert "111" in ALLOWED_CHAT_IDS
            assert "222" in ALLOWED_CHAT_IDS
        finally:
            ALLOWED_CHAT_IDS.clear()


# ---------------------------------------------------------------------------
#  Reply chunking (< 4096 chars per message)
# ---------------------------------------------------------------------------


class TestReplyChunking:
    def test_short_text_single_chunk(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter

        assert TelegramCommandCenter._chunk_text("hello", 4000) == ["hello"]

    def test_long_text_split_on_lines(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter

        text = "\n".join(f"line {i} " + "x" * 200 for i in range(100))
        chunks = TelegramCommandCenter._chunk_text(text, 4000)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 4000
        assert "".join(chunks) == text

    def test_oversized_single_line_hard_split(self) -> None:
        from scripts.telegram_commands import TelegramCommandCenter

        text = "y" * 9000
        chunks = TelegramCommandCenter._chunk_text(text, 4000)
        assert all(len(c) <= 4000 for c in chunks)
        assert "".join(chunks) == text


# ---------------------------------------------------------------------------
#  SafeGuard — LIVE mode counts exchange truth (live_positions.json)
# ---------------------------------------------------------------------------


class TestSafeGuardLiveCount:
    def test_live_mode_counts_live_positions_file(self, tmp_path: Any) -> None:
        from scripts.safety_limits import SafeGuard

        live_path = str(tmp_path / "live_positions.json")
        with open(live_path, "w") as f:
            f.write('{"BTC/USDT": {"symbol": "BTC/USDT", "quantity": 0.1},'
                    ' "ETH/USDT": {"symbol": "ETH/USDT", "quantity": 1.0}}')

        sg = SafeGuard(max_open_positions=1, live_mode=True)
        sg.set_live_positions_path(live_path)

        ok, reason = sg._check_max_open_positions()
        assert not ok
        assert "Max open positions reached: 2/1" in reason

    def test_live_mode_ignores_paper_positions_json(self, tmp_path: Any) -> None:
        """In LIVE mode a busy paper ledger must not block new entries."""
        from scripts.safety_limits import SafeGuard

        papers_path = str(tmp_path / "positions.json")
        with open(papers_path, "w") as f:
            f.write('{"positions": ['
                    '{"symbol": "DOGE/USDT", "status": "OPEN"},'
                    '{"symbol": "XRP/USDT", "status": "OPEN"}]}')
        live_path = str(tmp_path / "live_positions.json")
        with open(live_path, "w") as f:
            f.write("{}")

        sg = SafeGuard(max_open_positions=1, live_mode=True)
        sg.set_positions_path(papers_path)
        sg.set_live_positions_path(live_path)
        ok, reason = sg._check_max_open_positions()
        assert ok, reason

    def test_paper_mode_still_reads_positions_json(self, tmp_path: Any) -> None:
        from scripts.safety_limits import SafeGuard

        papers_path = str(tmp_path / "positions.json")
        with open(papers_path, "w") as f:
            f.write('{"positions": [{"symbol": "DOGE/USDT", "status": "OPEN"}]}')

        sg = SafeGuard(max_open_positions=1, live_mode=False)
        sg.set_positions_path(papers_path)
        ok, reason = sg._check_max_open_positions()
        assert not ok
        assert "1/1" in reason

    def test_missing_live_file_counts_zero(self, tmp_path: Any) -> None:
        from scripts.safety_limits import SafeGuard

        sg = SafeGuard(max_open_positions=1, live_mode=True)
        sg.set_live_positions_path(str(tmp_path / "nope.json"))
        ok, _ = sg._check_max_open_positions()
        assert ok


# ---------------------------------------------------------------------------
#  OrderManager retry — tag-less exchanges abort instead of resubmitting
# ---------------------------------------------------------------------------


class TestRetryAbortsOnTaglessExchange:
    class _FakeExchange:
        def fetch_open_orders(self, symbol: str) -> list:
            return []

        def fetch_closed_orders(self, symbol: str, limit: int = 20) -> list:
            return []

    def test_indodax_retry_raises_verification_error(self, tmp_path: Any) -> None:
        from scripts.order_manager import OrderManager, OrderVerificationError  # noqa: PLC0415
        from scripts.execution_engine import OrderRequest  # noqa: PLC0415

        class _TaglessProvider:
            name = "indodax"

            def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
                return {}

            def has(self, method: str) -> bool:
                return method == "fetchOpenOrders"

            def _get_exchange(self) -> Any:
                return TestRetryAbortsOnTaglessExchange._FakeExchange()

        class _Exchange:
            def get_provider(self) -> Any:
                return _TaglessProvider()

        om = OrderManager(
            config=MagicMock(), exchange=_Exchange(),
            wallet=MagicMock(), risk=MagicMock(), mode="LIVE",
        )
        request = OrderRequest(
            symbol="BTC/IDR", side="BUY", type="MARKET",
            amount=0.001, price=170_000_000.0,
        )
        with pytest.raises(OrderVerificationError, match="cannot tag orders"):
            om._find_existing_live_order(request)

    def test_tagging_exchange_still_looks_up(self) -> None:
        from scripts.order_manager import OrderManager  # noqa: PLC0415
        from scripts.execution_engine import OrderRequest  # noqa: PLC0415

        class _TaggingProvider:
            name = "binance"

            def client_order_id_params(self, client_order_id: str) -> dict[str, Any]:
                return {"newClientOrderId": client_order_id}

            def has(self, method: str) -> bool:
                return method == "fetchOpenOrders"

            def _get_exchange(self) -> Any:
                return TestRetryAbortsOnTaglessExchange._FakeExchange()

        class _Exchange:
            def get_provider(self) -> Any:
                return _TaggingProvider()

        om = OrderManager(
            config=MagicMock(), exchange=_Exchange(),
            wallet=MagicMock(), risk=MagicMock(), mode="LIVE",
        )
        request = OrderRequest(
            symbol="BTC/USDT", side="BUY", type="MARKET",
            amount=0.001, price=50_000.0,
        )
        # No matching order on the exchange -> None (retry would resubmit).
        assert om._find_existing_live_order(request) is None


# ---------------------------------------------------------------------------
#  Pipeline config propagation
# ---------------------------------------------------------------------------


class TestScannerTimeframePropagation:
    def test_scanner_config_overrides_include_timeframe(self) -> None:
        from scripts.pipeline import _CONFIG_OVERRIDES

        assert _CONFIG_OVERRIDES["scripts.scanner"]["TIMEFRAME"] == "timeframe"


# ---------------------------------------------------------------------------
#  /buy /sell command guards
# ---------------------------------------------------------------------------


def _services(mode: str = "PAPER", open_positions: list | None = None) -> Any:
    svc = MagicMock()
    svc.order.mode = mode
    svc.position.get_open_positions.return_value = open_positions or []
    svc.exchange.get_ticker.return_value = {"last": 100.0}
    svc.notification.notify_close.return_value = True
    svc.config.quote_currency = "USDT"
    return svc


def _ctx(services: Any) -> Any:
    from types import SimpleNamespace

    cfg = SimpleNamespace(quote_currency="USDT")
    return SimpleNamespace(services=services, config=cfg, read_json=lambda _: {})


class TestBuyDuplicateGuard:
    def test_paper_buy_rejects_when_symbol_already_open(self) -> None:
        from telegram.commands.buy import BuyCommand

        svc = _services(
            "PAPER",
            open_positions=[{"symbol": "BTC/USDT", "status": "OPEN"}],
        )
        result = BuyCommand().execute(_ctx(svc), "BTC/USDT 100")
        assert "already open" in result
        svc.order.execute.assert_not_called()

    def test_paper_buy_allows_fresh_symbol(self) -> None:
        from telegram.commands.buy import BuyCommand

        svc = _services("PAPER", open_positions=[])
        result = BuyCommand().execute(_ctx(svc), "ETH/USDT 100")
        assert "already open" not in result
        svc.order.execute.assert_called_once()


class TestSellPartialAndLive:
    def test_partial_amount_clamped_to_position(self) -> None:
        from telegram.commands.sell import SellCommand

        svc = _services(
            "PAPER",
            open_positions=[{
                "symbol": "BTC/USDT", "status": "OPEN",
                "remaining_qty": 0.05, "quantity": 0.05,
                "entry_price": 100.0, "current_price": 100.0,
            }],
        )
        SellCommand().execute(_ctx(svc), "BTC/USDT 999999")
        call = svc.order.execute.call_args[0][0]
        assert call.amount == 0.05  # clamped, cannot over-sell

    def test_live_sell_never_uses_paper_store(self) -> None:
        """LIVE /sell must resolve qty from live_positions (empty here),
        NOT from the paper ledger's fake position."""
        from telegram.commands.sell import SellCommand

        svc = _services(
            "LIVE",
            open_positions=[{  # paper ledger entry — must be IGNORED
                "symbol": "BTC/USDT", "status": "OPEN",
                "remaining_qty": 0.05, "quantity": 0.05,
            }],
        )
        svc.order.mode = "LIVE"
        # live_positions.json is empty -> no position to sell
        result = SellCommand().execute(_ctx(svc), "BTC/USDT")
        assert "No open position" in result
        svc.order.execute.assert_not_called()


# ---------------------------------------------------------------------------
#  Placeholder-free system commands
# ---------------------------------------------------------------------------


class TestNoPlaceholdersRemain:
    def test_restart_is_real_exec_path(self) -> None:
        import telegram.commands.restart as mod

        src = open(mod.__file__).read()
        assert "os.execv" in src
        assert "not implemented" not in src.lower()

    def test_reload_validates_not_stubbed(self) -> None:
        import telegram.commands.reload as mod

        src = open(mod.__file__).read()
        assert "load_config" in src
        assert "not implemented" not in src.lower()

    def test_stoploss_takeprofit_not_stubs(self) -> None:
        import telegram.commands.stoploss as sl
        import telegram.commands.takeprofit as tp

        for mod in (sl, tp):
            src = open(mod.__file__).read()
            assert "not implemented" not in src.lower()
        # percent and absolute-price parsing
        assert abs(sl._parse_value("5", 100.0, "sl") - 95.0) < 1e-9
        assert abs(sl._parse_value("5", 100.0, "tp") - 105.0) < 1e-9
        assert abs(sl._parse_value("9500", 10000.0, "sl") - 9500.0) < 1e-9