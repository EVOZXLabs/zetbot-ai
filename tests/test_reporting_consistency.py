"""Regression tests for reporting / accounting consistency fixes.

Covers:
  * Telegram Markdown escaping — dynamic content (AI insight lines, error
    messages, symbols) is escaped so the API can never reject a message
    with "can't parse entities", plus the plain-text resend fallback.
  * Quote-currency consistency — /summary and /performance use the
    configured quote currency instead of a hardcoded USDT.
  * Avg Hold — computed from entry_time → exit_time (position record),
    never a meaningless "0s" derived from the closing order's own fill
    timestamp; not shown at all when no entry data exists.
  * Pipeline report (main._build_summary) and HEALTH snapshot read the
    canonical MetricsManager account snapshot (realized + unrealized)
    instead of stale raw paper_balance.json keys.
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts.app_config import AppConfig
from telegram.formatter import (
    fmt_holding, md_escape, order_hold_seconds, parse_ts,
)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in a throwaway directory.

    Some commands read files via hardcoded CWD-relative paths
    (``open("data/...")``) rather than ``ctx.read_json``, so the sandbox
    must also redirect the working directory.
    """
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _cfg(quote: str = "USDT") -> AppConfig:
    return AppConfig(quote_currency=quote)


def _write_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fields = ["id", "symbol", "side", "quantity", "entry_price",
              "fill_price", "exit_price", "entry_fee", "exit_fee",
              "net_pnl", "net_pnl_pct", "created_at", "filled_at", "closed_at"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def _make_ctx(quote: str = "USDT"):
    """Real CommandContext whose read_json reads the sandboxed data dir."""
    from telegram.context import CommandContext
    ctx = CommandContext(config=_cfg(quote))
    return ctx


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
#  md_escape
# ---------------------------------------------------------------------------

class TestMdEscape:

    def test_plain_text_unchanged(self) -> None:
        assert md_escape("PRIME/IDR") == "PRIME/IDR"
        assert md_escape("hello world 123") == "hello world 123"
        assert md_escape("1.50%") == "1\\.50%"

    def test_specials_escaped(self) -> None:
        assert md_escape("ema_200") == "ema\\_200"
        assert md_escape("(score 78)") == "\\(score 78\\)"
        assert md_escape("*x*") == "\\*x\\*"
        assert md_escape("a`b") == "a\\`b"

    def test_escaped_text_has_no_unescaped_specials(self) -> None:
        specials = "*_[](){}#+-.=~!|>`\\"
        escaped = md_escape("buy_ema *fast* (x) [y] {z} #1 -2 +3 =4 ~5 |6 .7 !8 >9 `10 \\11")
        i = 0
        while i < len(escaped):
            if escaped[i] == "\\":
                i += 2  # escaped character — safe
                continue
            assert escaped[i] not in specials
            i += 1


# ---------------------------------------------------------------------------
#  order_hold_seconds
# ---------------------------------------------------------------------------

class TestOrderHoldSeconds:

    ENTRY = "2026-08-04T21:23:11.399571+00:00"
    EXIT = "2026-08-05T12:02:12.847441+00:00"

    def test_entry_time_to_exit_time(self) -> None:
        o = {"symbol": "X/IDR", "entry_time": self.ENTRY, "exit_time": self.EXIT}
        hold = order_hold_seconds(o)
        assert hold == pytest.approx(52741.44787, abs=0.01)

    def test_uses_position_opened_at_not_order_fill_time(self) -> None:
        # Engine-written closing order: filled_at == closed_at (both the
        # exit moment) — naive filled→closed math gives ~0s ("0s" bug).
        o = {"symbol": "PRIME/IDR", "filled_at": self.EXIT, "closed_at": self.EXIT}
        assert order_hold_seconds(o) is None
        # With the position record's opened_at, the real hold appears.
        hold = order_hold_seconds(
            o, {"PRIME/IDR": {"opened_at": self.ENTRY}},
        )
        assert hold == pytest.approx(52741.44787, abs=0.01)

    def test_none_when_no_entry_data(self) -> None:
        # filled_at on a closing order is the exit moment, so it is never
        # used as a stand-in for entry — the hold is simply unknown.
        o = {"symbol": "X", "filled_at": "2026-08-01T00:00:00+00:00",
             "closed_at": "2026-08-02T00:00:00+00:00"}
        assert order_hold_seconds(o) is None
        assert order_hold_seconds({"symbol": "X"}) is None
        assert order_hold_seconds({"symbol": "X", "closed_at": "garbage"}) is None

    def test_parse_ts_z_suffix(self) -> None:
        dt = parse_ts("2026-08-05T12:02:12.847441Z")
        assert dt is not None and dt.tzinfo is not None


# ---------------------------------------------------------------------------
#  /summary — avg hold from entry record + quote currency
# ---------------------------------------------------------------------------

class TestSummaryCommand:

    def _seed(self, now: datetime, opened_at: str, net_pnl: str = "298.57") -> None:
        _write_csv(
            "data/paper_trade_history.csv",
            [{
                "id": "po_abc-PRIME/IDR", "symbol": "PRIME/IDR", "side": "SELL",
                "net_pnl": net_pnl, "net_pnl_pct": "1.99",
                "created_at": now.isoformat(), "filled_at": now.isoformat(),
                "closed_at": now.isoformat(),
            }],
        )
        _write_json(
            "data/positions.json",
            {"positions": [{"symbol": "PRIME/IDR", "status": "CLOSED",
                            "opened_at": opened_at}]},
        )

    def test_avg_hold_uses_position_entry_not_exit_fill(self) -> None:
        now = _now()
        self._seed(now, (now - timedelta(days=2)).isoformat())
        from telegram.commands.summary import SummaryCommand
        out = SummaryCommand().execute(_make_ctx(), "")
        assert "Avg Hold: 2d" in out, out

    def test_no_hold_line_without_entry_data(self) -> None:
        now = _now()
        _write_csv("data/paper_trade_history.csv", [{
            "id": "po_abc-PRIME/IDR", "symbol": "PRIME/IDR", "side": "SELL",
            "net_pnl": "298.57", "net_pnl_pct": "1.99",
            "created_at": now.isoformat(), "filled_at": now.isoformat(),
            "closed_at": now.isoformat(),
        }])
        from telegram.commands.summary import SummaryCommand
        out = SummaryCommand().execute(_make_ctx(), "")
        assert "Avg Hold:" not in out, out

    def test_quote_currency_used(self) -> None:
        now = _now()
        self._seed(now, (now - timedelta(days=2)).isoformat(), net_pnl="1500.00")
        from telegram.commands.summary import SummaryCommand
        out = SummaryCommand().execute(_make_ctx(quote="IDR"), "")
        assert "1.50K IDR" in out, out
        assert "USDT" not in out, out


# ---------------------------------------------------------------------------
#  /performance — avg hold + quote currency
# ---------------------------------------------------------------------------

class TestPerformanceCommand:

    def _seed(self, hold_from_entry: bool = True) -> None:
        now = _now()
        two_days_ago = now - timedelta(days=2)
        order = {
            "id": "po_1", "symbol": "PRIME/IDR", "side": "SELL",
            "status": "CLOSED", "net_pnl": 100.0, "net_pnl_pct": 1.0,
            "entry_price": 100.0, "exit_price": 101.0,
            "closed_at": now.isoformat(), "filled_at": now.isoformat(),
        }
        _write_json("data/paper_orders.json", {"orders": [order]})
        _write_json("data/paper_balance.json",
                    {"initial_balance": 10000.0, "final_balance": 10100.0})
        if hold_from_entry:
            _write_json("data/positions.json",
                        {"positions": [{"symbol": "PRIME/IDR", "status": "CLOSED",
                                        "opened_at": two_days_ago.isoformat()}]})

    def test_avg_hold_from_position_entry(self) -> None:
        self._seed()
        from telegram.commands.performance import PerformanceCommand
        out = PerformanceCommand().execute(_make_ctx(), "")
        assert "Avg Hold: 2d" in out, out
        assert "0s" not in out, out

    def test_no_hold_line_without_entry_data(self) -> None:
        self._seed(hold_from_entry=False)
        from telegram.commands.performance import PerformanceCommand
        out = PerformanceCommand().execute(_make_ctx(), "")
        assert "Avg Hold:" not in out, out

    def test_quote_currency_used(self) -> None:
        self._seed()
        from telegram.commands.performance import PerformanceCommand
        out = PerformanceCommand().execute(_make_ctx(quote="IDR"), "")
        assert "IDR" in out, out
        assert " USDT" not in out, out


# ---------------------------------------------------------------------------
#  Pipeline report (main._build_summary) — canonical snapshot
# ---------------------------------------------------------------------------

class TestPipelineReportCanonical:

    def test_net_pnl_from_canonical_snapshot(self) -> None:
        # Raw keys are stale/absent — only realized_pnl exists.
        _write_json("data/paper_balance.json", {
            "initial_balance": 1000000.0,
            "final_balance": 965253.18,
            "realized_pnl": 298.57,
        })
        # Open position carries unrealized PnL (canonical source).
        _write_json("data/positions.json", {"positions": [{
            "symbol": "PRIME/IDR", "status": "OPEN",
            "quantity": 7.92211, "remaining_qty": 7.92211,
            "current_price": 4432.0, "unrealized_pnl": 298.57,
        }]})
        from main import _build_summary
        lines = _build_summary([], _cfg())
        text = "\n".join(lines)
        # net_pnl = realized + unrealized, computed from raw data — the
        # raw file has no net_pnl key at all.
        assert "Net PnL" in text and "+597.14" in text, text
        assert "Unrealized PnL" in text and "+298.57" in text, text
        # equity = cash + market value of open positions.
        assert "Equity" in text and "1,000,363.97" in text, text

    def test_raw_fallback_when_metrics_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_json("data/paper_balance.json", {
            "initial_balance": 10000.0,
            "final_balance": 9500.0,
            "realized_pnl": -500.0,
            "net_pnl": -500.0,
            "win_rate": 40.0,
            "total_trades": 5,
        })
        _write_json("data/positions.json", {"positions": []})
        import main as main_mod
        # Force the MetricsManager import inside _build_summary to fail so
        # the raw-key fallback branch runs.
        monkeypatch.setitem(main_mod.sys.modules, "scripts.metrics_manager", None)
        lines = main_mod._build_summary([], _cfg())
        text = "\n".join(lines)
        assert "Net PnL" in text and "-500.00" in text, text


# ---------------------------------------------------------------------------
#  HEALTH snapshot — canonical balance/equity/net_pnl
# ---------------------------------------------------------------------------

class TestHealthSnapshotCanonical:

    def test_health_uses_canonical_snapshot(self) -> None:
        _write_json("data/paper_balance.json", {
            "initial_balance": 1000000.0,
            "final_balance": 965253.18,
            "final_equity": 965253.18,   # stale — closure-time value
            "realized_pnl": 298.57,
        })
        _write_json("data/positions.json", {"positions": [{
            "symbol": "PRIME/IDR", "status": "OPEN",
            "quantity": 7.92211, "remaining_qty": 7.92211,
            "current_price": 4432.0, "unrealized_pnl": 298.57,
        }]})
        from scripts.health import HealthMonitor

        class _Logger:
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass
            def debug(self, *a, **k): pass

        monitor = HealthMonitor(logger=_Logger(), config=_cfg(), interval=60.0)
        monitor._config = replace(monitor._config, data_dir="data")
        with (
            patch("scripts.health._check_internet", return_value=(True, 1.0)),
            patch("scripts.health._check_exchange", return_value=(True, "binance")),
        ):
            snap = monitor._gather()
        assert snap["net_pnl"] == pytest.approx(597.14, abs=0.01)
        assert snap["balance"] == pytest.approx(965253.18, abs=0.01)
        assert snap["equity"] == pytest.approx(1000363.97, abs=0.01)


# ---------------------------------------------------------------------------
#  Notifier — plain-text resend on Markdown parse rejection
# ---------------------------------------------------------------------------

class TestNotifierParseFallback:

    def test_resends_plain_text_on_parse_error(self) -> None:
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=3)

        fail = requests.RequestException("400 parse")
        fail.response = MagicMock(status_code=400, text="Bad Request: can't parse entities")
        ok = requests.Response()
        ok.status_code = 200

        calls: list = []

        def _post(*args, **kwargs):
            calls.append(dict(kwargs["json"]))
            if len(calls) == 1:
                raise fail
            return ok

        with patch("bot.notifier.requests.post", side_effect=_post) as mock_post:
            assert n._send("*needs* parsing") is True
        assert mock_post.call_count == 2
        assert calls[0]["parse_mode"] == "Markdown"
        assert "parse_mode" not in calls[1]

    def test_retries_normally_when_not_a_parse_error(self) -> None:
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c", max_retry=2)
        fail = requests.ConnectionError("network down")
        fail.response = None
        ok = requests.Response()
        ok.status_code = 200

        calls: list = []

        def _post(*args, **kwargs):
            calls.append(dict(kwargs["json"]))
            if len(calls) < 2:
                raise fail
            return ok

        with patch("bot.notifier.requests.post", side_effect=_post) as mock_post:
            assert n._send("hello") is True
        assert mock_post.call_count == 2
        # parse_mode is kept on non-parse failures.
        assert all("parse_mode" in c for c in calls)

    def test_error_message_content_is_escaped(self) -> None:
        from bot.notifier import Notifier
        n = Notifier(enabled=True, token="t", chat_id="c")
        with patch.object(n, "_send") as mock_send:
            n.notify_error("boom * here ` and _there")
        text = mock_send.call_args[0][0]
        assert "boom \\* here \\` and \\_there" in text
