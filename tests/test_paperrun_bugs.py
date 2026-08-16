"""Regression tests for 3 bugs found during paper run (post-commit 7aab135).

Bug 1 — POSITION_CLOSED notification uses wrong currency (hardcoded USDT)
        for IDR-quoted exchanges.

Bug 2 — Telegram Markdown fallback still returns 400: plain-text retry
        must disable parse_mode AND strip Markdown markup from the text.

Bug 3 — HEALTH net_pnl resets to +0.00 after a losing close because
        balance_json() merge guard compared realized_pnl with ``>``
        (sign-sensitive), letting the engine's 0.0 overwrite the
        monitor's -2066.46.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _make_notifier(quote_currency: str = "USDT", **kwargs: object):
    from bot.notifier import Notifier

    defaults: dict[str, object] = {
        "enabled": True,
        "token": "123:ABC",
        "chat_id": "456",
        "timeout": 10,
        "max_retry": 3,
        "testing": False,
        "quote_currency": quote_currency,
    }
    defaults.update(kwargs)
    return Notifier(**defaults)


def _parse_reject_exc(status: int = 400, body: str = "Bad Request: can't parse entities"):
    """Build a RequestException that looks like a Telegram 400 parse rejection."""
    exc = requests.exceptions.HTTPError(body)
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    exc.response = resp
    return exc


# ===========================================================================
#  BUG 1 — POSITION_CLOSED notification currency
# ===========================================================================


class TestPositionClosedCurrency:
    """Telegram message AND internal log must use canonical quote currency."""

    def test_idr_pair_shows_idr_in_message(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="PRIME/IDR",
                entry_price=4000.0,
                exit_price=4200.0,
                pnl=9376.18,
                pnl_pct=2.34,
                balance=1_000_000.0,
                exit_reason="Take Profit",
                holding_time=timedelta(hours=5),
            )
        text = mock_send.call_args[0][0]
        assert "IDR" in text, f"Expected IDR in message, got:\n{text}"
        assert "USDT" not in text, f"Unexpected USDT in message:\n{text}"

    def test_idr_pair_log_uses_idr_not_usdt(self) -> None:
        """Logger.info line must NOT say USDT for an IDR-quoted symbol."""
        n = _make_notifier(quote_currency="IDR")
        import logging
        with (
            patch.object(n, "_send"),
            patch.object(logging.getLogger("ZetBot"), "info") as mock_log,
        ):
            n.notify_position_closed(
                symbol="PRIME/IDR",
                pnl=-2066.46,
            )
        # Find the POSITION_CLOSED log call
        calls = [str(c) for c in mock_log.call_args_list]
        pos_closed_calls = [c for c in calls if "POSITION_CLOSED" in c]
        assert pos_closed_calls, "No POSITION_CLOSED log line found"
        log_line = pos_closed_calls[0]
        assert "IDR" in log_line, f"Log says USDT instead of IDR:\n{log_line}"
        assert "USDT" not in log_line, f"Log still says USDT:\n{log_line}"

    def test_usdt_pair_shows_usdt(self) -> None:
        n = _make_notifier(quote_currency="USDT")
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="BTC/USDT",
                entry_price=50_000.0,
                exit_price=51_000.0,
                pnl=150.0,
                pnl_pct=3.0,
                balance=10_150.0,
                exit_reason="Take Profit",
                holding_time=timedelta(hours=4),
            )
        text = mock_send.call_args[0][0]
        assert "USDT" in text

    def test_notify_take_profit_uses_symbol_quote(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_take_profit(
                symbol="PRIME/IDR",
                entry_price=4000.0,
                exit_price=4400.0,
                profit=9376.18,
                holding_time=timedelta(hours=3),
            )
        text = mock_send.call_args[0][0]
        assert "IDR" in text, text
        assert "USDT" not in text, text

    def test_notify_stop_loss_uses_symbol_quote(self) -> None:
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_stop_loss(
                symbol="PRIME/IDR",
                entry_price=4000.0,
                exit_price=3800.0,
                loss=-2066.46,
                holding_time=timedelta(hours=1),
            )
        text = mock_send.call_args[0][0]
        assert "IDR" in text, text
        assert "USDT" not in text, text

    def test_fallback_when_no_slash_in_symbol(self) -> None:
        """When symbol has no '/', falls back to configured quote_currency."""
        n = _make_notifier(quote_currency="IDR")
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed(
                symbol="PRIMEONLY",
                pnl=-100.0,
                balance=999_900.0,
            )
        text = mock_send.call_args[0][0]
        assert "IDR" in text, text

    def test_from_config_reads_quote_currency(self) -> None:
        """Notifier.from_config() must pass quote_currency through."""
        from bot.notifier import Notifier
        cfg = MagicMock()
        cfg.telegram_enabled = True
        cfg.telegram_token = "t"
        cfg.telegram_chat_id = "c"
        cfg.telegram_timeout = 5
        cfg.telegram_retry = 2
        cfg.testing = False
        cfg.quote_currency = "IDR"
        n = Notifier.from_config(cfg)
        assert n._quote_currency == "IDR"


# ===========================================================================
#  BUG 2 — Markdown fallback disables parse_mode AND strips markup
# ===========================================================================


class TestMarkdownFallback:
    """Plain-text retry must send plain text with no parse_mode key."""

    def test_parse_error_triggers_plain_text_retry(self) -> None:
        """On Markdown rejection, retry immediately with no parse_mode."""
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=3)

        calls: list[dict] = []
        exc = _parse_reject_exc()

        def _post(*args, **kwargs):
            calls.append(dict(kwargs["json"]))
            if len(calls) == 1:
                raise exc
            r = requests.Response()
            r.status_code = 200
            return r

        with patch("bot.notifier.requests.post", side_effect=_post):
            result = n._send("*bold* and _italic_")

        assert result is True
        assert len(calls) == 2
        # First call had parse_mode
        assert calls[0].get("parse_mode") == "Markdown"
        # Second call must NOT have parse_mode
        assert "parse_mode" not in calls[1], f"parse_mode still present: {calls[1]}"

    def test_plain_text_retry_strips_markdown_from_text(self) -> None:
        """Fallback text must be stripped of Markdown markup."""
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=3)

        sent_texts: list[str] = []
        exc = _parse_reject_exc()

        def _post(*args, **kwargs):
            sent_texts.append(kwargs["json"]["text"])
            if len(sent_texts) == 1:
                raise exc
            r = requests.Response()
            r.status_code = 200
            return r

        raw = "*POSITION CLOSED — PRIME/IDR*\nProfit 9376.18 IDR"
        with patch("bot.notifier.requests.post", side_effect=_post):
            n._send(raw)

        assert len(sent_texts) == 2
        plain = sent_texts[1]
        # Markdown asterisks must be gone
        assert "*" not in plain, f"Asterisks remain in plain text: {plain!r}"
        # Content must survive
        assert "POSITION CLOSED" in plain
        assert "PRIME/IDR" in plain

    def test_special_chars_stripped_in_fallback(self) -> None:
        """All Markdown specials are stripped: _ * ` [ ] ( )."""
        from bot.notifier import Notifier

        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=3)
        exc = _parse_reject_exc()
        received: list[str] = []

        def _post(*args, **kwargs):
            received.append(kwargs["json"]["text"])
            if len(received) == 1:
                raise exc
            r = requests.Response()
            r.status_code = 200
            return r

        msg = "*bold* _italic_ `code` [link](http://x.com) ```block\nhello\n```"
        with patch("bot.notifier.requests.post", side_effect=_post):
            n._send(msg)

        plain = received[1]
        # No Markdown markup characters should remain as control syntax
        assert "*" not in plain
        assert "`" not in plain
        # Underscore is used in plain text too, but the italic markup is gone
        assert "_italic_" not in plain
        # Link text survives, URL is dropped
        assert "link" in plain
        # Block content survives
        assert "hello" in plain

    def test_strip_markdown_method_directly(self) -> None:
        """_strip_markdown produces clean plain text from Markdown input."""
        from bot.notifier import Notifier
        strip = Notifier._strip_markdown

        assert strip("*bold*") == "bold"
        assert strip("_italic_") == "italic"
        assert strip("`code`") == "code"
        assert strip("```\nblock\n```") == "block\n"
        assert strip("[text](http://example.com)") == "text"
        # Backslash-escaped specials are unwound
        assert strip(r"1\.50%") == "1.50%"
        # Normal text unchanged
        assert strip("hello world 123") == "hello world 123"

    def test_non_parse_error_keeps_parse_mode_on_retry(self) -> None:
        """A network error (not a parse error) retries with parse_mode intact."""
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=2)

        calls: list[dict] = []
        net_err = requests.ConnectionError("network down")
        net_err.response = None

        def _post(*args, **kwargs):
            calls.append(dict(kwargs["json"]))
            if len(calls) < 2:
                raise net_err
            r = requests.Response()
            r.status_code = 200
            return r

        with patch("bot.notifier.requests.post", side_effect=_post):
            n._send("hello")

        assert len(calls) == 2
        # parse_mode must be present on both attempts
        assert all("parse_mode" in c for c in calls)

    def test_position_closed_message_survives_parse_error(self) -> None:
        """Full notify_position_closed flow: user still gets the message."""
        n = _make_notifier(quote_currency="IDR")
        exc = _parse_reject_exc()
        calls: list[dict] = []

        def _post(*args, **kwargs):
            calls.append(dict(kwargs["json"]))
            if len(calls) == 1:
                raise exc
            r = requests.Response()
            r.status_code = 200
            return r

        with patch("bot.notifier.requests.post", side_effect=_post):
            result = n.notify_position_closed(
                symbol="PRIME/IDR",
                entry_price=4000.0,
                exit_price=4200.0,
                pnl=9376.18,
                pnl_pct=2.34,
                balance=1_000_000.0,
                exit_reason="Take Profit",
                holding_time=timedelta(hours=5),
            )

        assert result is True, "notify_position_closed should succeed via fallback"
        assert len(calls) == 2
        # Fallback has no parse_mode
        assert "parse_mode" not in calls[1]
        # Message still contains trade info
        assert "POSITION CLOSED" in calls[1]["text"]
        assert "IDR" in calls[1]["text"]


# ===========================================================================
#  BUG 3 — HEALTH net_pnl resets to +0.00 after a losing close
# ===========================================================================


@pytest.fixture()
def _data_dir(tmp_path: "pytest.Path", monkeypatch: "pytest.MonkeyPatch") -> str:
    """Provide a clean temp data directory and chdir there."""
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    return str(data)


def _write_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class TestNetPnlAfterLossClose:
    """net_pnl must persist correctly after a losing trade is closed."""

    def test_engine_balance_json_preserves_negative_realized_pnl(
        self, _data_dir: str
    ) -> None:
        """balance_json() must not reset realized_pnl to 0 after a loss.

        Scenario:
          - Monitor closes a losing position → writes realized_pnl=-2066.46
          - Engine runs (no new orders) → must NOT overwrite with 0.0
        """
        from scripts.paper_trading_engine import PaperExport, EquitySnapshot

        balance_path = os.path.join(_data_dir, "paper_balance.json")
        positions_path = os.path.join(_data_dir, "positions.json")

        # Simulate monitor having already written the loss to the file
        _write_json(balance_path, {
            "initial_balance": 1_000_000.0,
            "final_balance": 997_933.54,
            "realized_pnl": -2066.46,
            "total_trades": 1,
            "winning_trades": 0,
            "losing_trades": 1,
            "win_rate": 0.0,
        })
        _write_json(positions_path, {"positions": []})

        # Engine metrics have 0 closed orders (hasn't loaded the monitor's trade)
        engine_metrics: dict = {
            "initial_balance": 1_000_000.0,
            "final_balance": 997_933.54,
            "final_equity": 997_933.54,
            "total_return_pct": -0.21,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
        }

        PaperExport.balance_json(engine_metrics, [], balance_path)

        with open(balance_path) as f:
            result = json.load(f)

        # The monitor's realized_pnl must survive
        assert result["realized_pnl"] == pytest.approx(-2066.46, abs=0.01), (
            f"realized_pnl was overwritten: {result['realized_pnl']}"
        )
        # net_pnl = realized + unrealized (no open positions)
        assert result["net_pnl"] == pytest.approx(-2066.46, abs=0.01), (
            f"net_pnl should equal realized_pnl after close: {result['net_pnl']}"
        )
        # trade counts must be preserved
        assert result["total_trades"] == 1

    def test_engine_balance_json_preserves_positive_realized_pnl(
        self, _data_dir: str
    ) -> None:
        """Regression: positive realized_pnl was already handled; keep it working."""
        from scripts.paper_trading_engine import PaperExport

        balance_path = os.path.join(_data_dir, "paper_balance.json")
        positions_path = os.path.join(_data_dir, "positions.json")

        _write_json(balance_path, {
            "initial_balance": 10_000.0,
            "final_balance": 10_298.57,
            "realized_pnl": 298.57,
            "total_trades": 1,
            "winning_trades": 1,
            "losing_trades": 0,
            "win_rate": 100.0,
        })
        _write_json(positions_path, {"positions": []})

        engine_metrics: dict = {
            "initial_balance": 10_000.0,
            "final_balance": 10_298.57,
            "final_equity": 10_298.57,
            "total_return_pct": 2.99,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
        }

        from scripts.paper_trading_engine import PaperExport
        PaperExport.balance_json(engine_metrics, [], balance_path)

        with open(balance_path) as f:
            result = json.load(f)

        assert result["realized_pnl"] == pytest.approx(298.57, abs=0.01)
        assert result["net_pnl"] == pytest.approx(298.57, abs=0.01)

    def test_health_net_pnl_is_total_realized_session_pnl(
        self, _data_dir: str
    ) -> None:
        """HEALTH net_pnl = realized_pnl + unrealized_pnl (session total).

        After a position closes and no new position is open:
          net_pnl = realized_pnl + 0 = realized_pnl
        Both positive and negative values must be preserved.
        """
        from scripts.health import HealthMonitor
        from scripts.app_config import AppConfig
        from dataclasses import replace

        # Write realistic post-close state: losing trade, nothing open.
        _write_json(os.path.join(_data_dir, "paper_balance.json"), {
            "initial_balance": 1_000_000.0,
            "final_balance": 997_933.54,
            "final_equity": 997_933.54,
            "realized_pnl": -2066.46,
            "unrealized_pnl": 0.0,
            "net_pnl": -2066.46,
        })
        _write_json(os.path.join(_data_dir, "positions.json"), {"positions": []})

        class _Logger:
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass
            def debug(self, *a, **k): pass

        cfg = AppConfig(quote_currency="IDR")
        monitor = HealthMonitor(logger=_Logger(), config=cfg, interval=60.0)
        monitor._config = replace(monitor._config, data_dir=_data_dir)

        with (
            patch("scripts.health._check_internet", return_value=(True, 1.0)),
            patch("scripts.health._check_exchange", return_value=(True, "indodax")),
        ):
            snap = monitor._gather()

        assert snap["net_pnl"] == pytest.approx(-2066.46, abs=0.01), (
            f"HEALTH net_pnl should be -2066.46 after losing close, got {snap['net_pnl']}"
        )

    def test_health_snapshot_includes_quote_currency(self, _data_dir: str) -> None:
        """HEALTH snapshot must include quote_currency so log is human-readable."""
        from scripts.health import HealthMonitor
        from scripts.app_config import AppConfig
        from dataclasses import replace

        _write_json(os.path.join(_data_dir, "paper_balance.json"), {
            "initial_balance": 1_000_000.0,
            "final_balance": 997_933.54,
            "realized_pnl": -2066.46,
        })
        _write_json(os.path.join(_data_dir, "positions.json"), {"positions": []})

        class _Logger:
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass
            def debug(self, *a, **k): pass

        cfg = AppConfig(quote_currency="IDR")
        monitor = HealthMonitor(logger=_Logger(), config=cfg, interval=60.0)
        monitor._config = replace(monitor._config, data_dir=_data_dir)

        with (
            patch("scripts.health._check_internet", return_value=(True, 1.0)),
            patch("scripts.health._check_exchange", return_value=(True, "indodax")),
        ):
            snap = monitor._gather()

        assert snap.get("quote_currency") == "IDR", (
            f"quote_currency missing or wrong in health snapshot: {snap}"
        )

    def test_health_log_format_includes_currency(self, _data_dir: str) -> None:
        """_format_metrics log line must include quote currency, not bare number."""
        from scripts.health import _format_metrics

        metrics = {
            "uptime_sec": 3600,
            "rss_kb": 102400,
            "thread_count": 12,
            "process_cpu_sec": 5.0,
            "internet_ok": True,
            "exchange_ok": True,
            "telegram_status": "OK",
            "unrealized_pnl": -2066.46,
            "quote_currency": "IDR",
        }
        line = _format_metrics(metrics)
        assert "IDR" in line, f"Currency missing from log line: {line}"
        assert "open_pnl=-2066.46 IDR" in line, f"Log line: {line}"
