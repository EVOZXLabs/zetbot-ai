"""
Reliability tests for the Telegram polling loop.

Covers: exponential backoff schedule (5s/10s/30s/60s), timeout /
connection / temporary-API failure recovery, daemon survival while
Telegram is offline, link health status (OK / DEGRADED / OFFLINE) and
health reporting integration.
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

from scripts.app_config import AppConfig


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> AppConfig:
    cfg = AppConfig(
        account_balance=10000,
        exchange="binance",
        timeframe="1h",
        telegram_timeout=10,
        telegram_retry=3,
    )
    return AppConfig(**{**cfg.__dict__, **overrides})


class _FakeShutdownEvent:
    """Shutdown-event stand-in whose wait() never blocks."""

    def __init__(self, is_set: bool = False) -> None:
        self._is_set = is_set

    def is_set(self) -> bool:
        return self._is_set

    def wait(self, timeout: float | None = None) -> bool:
        return self._is_set


class _FakeLogger:
    """In-memory logger for HealthMonitor tests."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"INFO {msg % args if args else msg}")

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"ERROR {msg % args if args else msg}")

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"WARN {msg % args if args else msg}")

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._lines.append(f"DEBUG {msg % args if args else msg}")


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate every test from the real data/ directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.telegram_commands.TELEGRAM_STATUS_FILE",
        str(tmp_path / "telegram_status.json"),
    )
    return tmp_path


def _make_center(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **cfg_overrides: Any):
    import scripts.telegram_commands as tgm

    center = tgm.TelegramCommandCenter(
        _make_config(data_dir=str(tmp_path), **cfg_overrides),
        test_mode=True,
        shutdown_event=None,
    )
    return center, tgm


def _status_file(tmp_path: Path) -> Path:
    return tmp_path / "telegram_status.json"


# ---------------------------------------------------------------------------
#  Backoff schedule
# ---------------------------------------------------------------------------


class TestRetryDelay:

    def test_schedule_matches_5_10_30_60(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        center, _ = _make_center(monkeypatch, tmp_path)
        for errors, expected in [
            (1, 5), (2, 10), (3, 30), (4, 60), (5, 60), (12, 60),
        ]:
            center._consecutive_errors = errors
            assert center._retry_delay() == expected

    def test_backoff_resets_after_recovery(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        center, _ = _make_center(monkeypatch, tmp_path)
        center._consecutive_errors = 3
        assert center._retry_delay() == 30
        center._record_success()
        assert center._consecutive_errors == 0
        assert center._retry_delay() == 5


# ---------------------------------------------------------------------------
#  Poll failure classification
# ---------------------------------------------------------------------------


class TestPollTelegram:

    def test_timeout_is_raised_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        def fake_get(*_a: Any, **_k: Any) -> Any:
            raise requests.Timeout("Read timed out")

        monkeypatch.setattr(tgm.requests, "get", fake_get)
        with pytest.raises(requests.Timeout):
            center._poll_telegram()

    def test_connection_error_is_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        def fake_get(*_a: Any, **_k: Any) -> Any:
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr(tgm.requests, "get", fake_get)
        with pytest.raises(requests.ConnectionError):
            center._poll_telegram()

    def test_http_error_is_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        class _Resp:
            def raise_for_status(self) -> None:
                raise requests.HTTPError("502 Bad Gateway")

            def json(self) -> dict:
                return {}

        monkeypatch.setattr(tgm.requests, "get", lambda *_a, **_k: _Resp())
        with pytest.raises(requests.HTTPError):
            center._poll_telegram()

    def test_api_error_response_raises_telegram_api_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"ok": False, "description": "Conflict: terminated by other getUpdates"}

        monkeypatch.setattr(tgm.requests, "get", lambda *_a, **_k: _Resp())
        with pytest.raises(tgm.TelegramAPIError):
            center._poll_telegram()

    def test_invalid_json_raises_telegram_api_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        class _Resp:
            text = "<html>not json</html>"

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

        monkeypatch.setattr(tgm.requests, "get", lambda *_a, **_k: _Resp())
        with pytest.raises(tgm.TelegramAPIError):
            center._poll_telegram()

    def test_success_returns_updates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"ok": True, "result": [{"update_id": 42, "message": {}}]}

        monkeypatch.setattr(tgm.requests, "get", lambda *_a, **_k: _Resp())
        updates = center._poll_telegram()
        assert updates == [{"update_id": 42, "message": {}}]


# ---------------------------------------------------------------------------
#  Failure / recovery state machine
# ---------------------------------------------------------------------------


class TestLinkHealth:

    def test_first_failure_degrades_and_writes_health(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        logs: list[str] = []
        monkeypatch.setattr(tgm, "_log", lambda msg: logs.append(msg))

        center._record_failure("timeout: Read timed out")

        assert center._consecutive_errors == 1
        assert center.link_status == "DEGRADED"
        assert any("connection lost, retry in 5s" in line for line in logs)

        payload = json.loads(_status_file(tmp_path).read_text())
        assert payload["status"] == "DEGRADED"
        assert payload["consecutive_errors"] == 1

    def test_five_failures_reach_offline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        monkeypatch.setattr(tgm, "_log", lambda msg: None)

        for _ in range(4):
            center._record_failure("timeout")
        assert center.link_status == "DEGRADED"

        center._record_failure("timeout")
        assert center.link_status == "OFFLINE"
        assert json.loads(_status_file(tmp_path).read_text())["status"] == "OFFLINE"

    def test_success_restores_connection_and_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        logs: list[str] = []
        monkeypatch.setattr(tgm, "_log", lambda msg: logs.append(msg))

        center._record_failure("timeout")
        center._record_failure("timeout")
        center._record_success()

        assert center._consecutive_errors == 0
        assert center.link_status == "OK"
        assert any("connection restored" in line for line in logs)
        assert json.loads(_status_file(tmp_path).read_text())["status"] == "OK"

    def test_success_without_prior_failures_is_quiet(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        logs: list[str] = []
        monkeypatch.setattr(tgm, "_log", lambda msg: logs.append(msg))

        center._record_success()
        assert not any("connection restored" in line for line in logs)
        assert center.link_status == "OK"

    def test_errors_never_stop_the_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Regression: the old circuit-breaker killed the poll loop after
        15 consecutive errors.  Telegram is a communication channel only —
        a prolonged outage must degrade status, never stop the daemon.
        """
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        center._consecutive_errors = 20
        center._record_failure("timeout")
        assert center._running is True
        assert center.link_status == "OFFLINE"

    def test_failure_respects_shutdown_during_backoff(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent(is_set=True)
        center._record_failure("timeout")
        assert center._running is False

    def test_health_status_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        center, _ = _make_center(monkeypatch, tmp_path)
        center._consecutive_errors = 7
        center._link_status = "OFFLINE"
        snap = center.health_status()
        assert snap["status"] == "OFFLINE"
        assert snap["consecutive_errors"] == 7


# ---------------------------------------------------------------------------
#  run() loop behaviour (daemon survival + recovery)
# ---------------------------------------------------------------------------


class TestRunLoop:

    def test_run_survives_persistent_connection_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        monkeypatch.setattr(tgm, "_log", lambda msg: None)

        calls: dict[str, int] = {"n": 0}

        def fake_poll() -> list[dict[str, Any]]:
            calls["n"] += 1
            raise requests.ConnectionError("simulated network down")

        center._poll = fake_poll  # type: ignore[assignment]
        thread = threading.Thread(target=center.run, daemon=True)
        thread.start()
        time.sleep(0.3)
        center.stop()
        thread.join(timeout=3.0)

        assert not thread.is_alive()
        assert calls["n"] > 5, "loop must keep retrying while offline"
        assert center.link_status == "OFFLINE"

    def test_run_recovers_after_timeouts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.telegram_commands as tgm

        center, _ = _make_center(monkeypatch, tmp_path)
        center._shutdown_event = _FakeShutdownEvent()
        monkeypatch.setattr(tgm, "_log", lambda msg: None)

        calls: dict[str, int] = {"n": 0}

        def fake_poll() -> list[dict[str, Any]]:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise requests.Timeout("Read timed out")
            return []

        center._poll = fake_poll  # type: ignore[assignment]
        thread = threading.Thread(target=center.run, daemon=True)
        thread.start()

        deadline = time.time() + 5.0
        while time.time() < deadline and center._consecutive_errors != 0:
            time.sleep(0.01)
        center.stop()
        thread.join(timeout=3.0)

        assert center._consecutive_errors == 0
        assert center.link_status == "OK"
        assert calls["n"] >= 3


# ---------------------------------------------------------------------------
#  Health reporting integration
# ---------------------------------------------------------------------------


class TestHealthReporting:

    def test_read_telegram_status_missing_is_unknown(
        self, tmp_path: Path,
    ) -> None:
        from scripts.health import _read_telegram_status
        assert _read_telegram_status(str(tmp_path / "nope.json")) == "UNKNOWN"

    def test_read_telegram_status_valid(
        self, tmp_path: Path,
    ) -> None:
        from scripts.health import _read_telegram_status
        (tmp_path / "telegram_status.json").write_text(json.dumps({"status": "DEGRADED"}))
        assert _read_telegram_status(str(tmp_path / "telegram_status.json")) == "DEGRADED"

    def test_read_telegram_status_unknown_value(
        self, tmp_path: Path,
    ) -> None:
        from scripts.health import _read_telegram_status
        (tmp_path / "telegram_status.json").write_text(json.dumps({"status": "BOGUS"}))
        assert _read_telegram_status(str(tmp_path / "telegram_status.json")) == "UNKNOWN"

    def test_health_gather_includes_telegram_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.health as health

        monkeypatch.setattr(health, "_check_internet", lambda: (True, 1.0))
        monkeypatch.setattr(health, "_check_exchange", lambda name: (True, "binance"))
        (tmp_path / "telegram_status.json").write_text(json.dumps({"status": "OFFLINE"}))

        cfg = _make_config(data_dir=str(tmp_path))
        monitor = health.HealthMonitor(logger=_FakeLogger(), config=cfg, interval=60.0)
        snapshot = monitor._gather()
        assert snapshot["telegram_status"] == "OFFLINE"

    def test_health_gather_unknown_when_no_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        import scripts.health as health

        monkeypatch.setattr(health, "_check_internet", lambda: (True, 1.0))
        monkeypatch.setattr(health, "_check_exchange", lambda name: (True, "binance"))

        cfg = _make_config(data_dir=str(tmp_path))
        monitor = health.HealthMonitor(logger=_FakeLogger(), config=cfg, interval=60.0)
        assert monitor._gather()["telegram_status"] == "UNKNOWN"

    def test_format_metrics_includes_telegram(
        self, tmp_path: Path,
    ) -> None:
        from scripts.health import _format_metrics
        base = {
            "uptime_sec": 3661,
            "rss_kb": 102400,
            "thread_count": 5,
            "process_cpu_sec": 12.5,
            "internet_ok": True,
            "exchange_ok": True,
        }
        assert "telegram=OFFLINE" in _format_metrics({**base, "telegram_status": "OFFLINE"})
        assert "telegram=UNKNOWN" in _format_metrics(base)


# ---------------------------------------------------------------------------
#  /health command display
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: Any):
    from telegram.context import CommandContext
    from unittest.mock import MagicMock

    config = _make_config(telegram_token="t", telegram_chat_id="c")
    ctx = CommandContext(
        config=config,
        logger=_FakeLogger(),
        chat_id="c",
        message_id=0,
        update_id=0,
        raw_text="/health",
        is_admin=True,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _health_snapshot(status: str | None = None) -> dict[str, Any]:
    snap = {
        "internet_ok": True,
        "exchange_ok": True,
        "scanner_status": "healthy",
        "telegram_status": status,
    }
    return {k: v for k, v in snap.items() if v is not None}


class TestHealthCommandDisplay:

    def _execute(self, status: str | None, expected: str, icon: str) -> None:
        from unittest.mock import MagicMock

        from telegram.commands.health import HealthCommand

        health = MagicMock()
        health.force_refresh.return_value = _health_snapshot(status)
        ctx = _make_ctx(health_monitor=health)
        result = HealthCommand().execute(ctx, "")
        assert expected in result
        assert icon in result

    def test_telegram_ok(self) -> None:
        self._execute("OK", "Telegram — OK", "🟢")

    def test_telegram_degraded(self) -> None:
        self._execute("DEGRADED", "Telegram — DEGRADED", "🟡")

    def test_telegram_offline(self) -> None:
        self._execute("OFFLINE", "Telegram — OFFLINE", "🔴")

    def test_fallback_when_no_status_reported(self) -> None:
        from unittest.mock import MagicMock

        from telegram.commands.health import HealthCommand

        health = MagicMock()
        health.force_refresh.return_value = _health_snapshot(None)
        ctx = _make_ctx(health_monitor=health)
        result = HealthCommand().execute(ctx, "")
        assert "Telegram — Disconnected" in result
        assert "Telegram" in result
