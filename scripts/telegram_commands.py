"""
Telegram Command Center for ZetBot AI.

Long-polls the Telegram Bot API for commands and dispatches them to
existing pipeline modules.  Runs as a standalone daemon.

Usage::

    python -m scripts.telegram_commands

TEST_MODE (no Telegram credentials required)::

    TEST_MODE=true python -m scripts.telegram_commands

Commands::

    /status     — Bot status, balance, positions overview
    /pipeline   — Execute the full analysis pipeline
    /scan       — Run market scanner only
    /positions  — Show all open positions with details
    /balance    — Account balance & equity
    /summary    — Today's trading statistics
    /pause      — Disable new trade openings
    /resume     — Enable new trade openings
    /health     — System health & status overview
    /version    — Show ZetBot version information
    /shutdown   — Shut down the bot gracefully
    /help       — List all available commands
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from scripts.app_config import AppConfig, load_config

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 5           # short poll for responsive shutdown
POLL_REQUEST_TIMEOUT = 10  # HTTP request timeout (must exceed POLL_TIMEOUT)
PAUSE_FILE = "data/.paused"
SHUTDOWN_FILE = "data/.shutdown_requested"
UPDATE_ID_FILE = "data/.last_update_id"
STARTUP_GRACE_PERIOD = 30  # ignore shutdown commands within N seconds of start
DATA_DIR = "data"

# Poll retry schedule (seconds): 1st failure -> 5s, 2nd -> 10s,
# 3rd -> 30s, then capped at 60s until the connection is restored.
RETRY_DELAYS = [5, 10, 30, 60]

# Consecutive failures before the link health degrades.
DEGRADED_ERRORS = 1        # 1+ failures  -> telegram=DEGRADED
OFFLINE_ERRORS = 5         # 5+ failures  -> telegram=OFFLINE

HEALTH_WRITE_INTERVAL = 30  # throttle telegram_status.json writes (seconds)
TELEGRAM_STATUS_FILE = "data/telegram_status.json"

BOT_VERSION = "v0.5.0"

# Test-mode simulated command sequence
_TEST_COMMANDS = [
    "/help",
    "/status",
    "/health",
    "/balance",
    "/positions",
    "/summary",
    "/pause",
    "/status",
    "/resume",
    "/status",
    "/unknown",
]

# ---------------------------------------------------------------------------
#  Logger helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [TG] {msg}", flush=True)


# ---------------------------------------------------------------------------
#  JSON reader
# ---------------------------------------------------------------------------


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class TelegramAPIError(Exception):
    """Transient Telegram API-level failure (not a transport error).

    Raised when Telegram returns an error response (``ok=false``, HTTP
    error status, invalid JSON body).  It is never fatal — the polling
    loop treats it like a connection problem and backs off.
    """


# ---------------------------------------------------------------------------
#  Command Center
# ---------------------------------------------------------------------------


class TelegramCommandCenter:
    """Long-poll Telegram and dispatch commands to the ZetBot pipeline."""

    def __init__(
        self,
        config: AppConfig,
        test_mode: bool = False,
        health_monitor: Any = None,
        shutdown_event: Any = None,
        pid_file: Any = None,
        services: Any = None,
    ) -> None:
        self._config = config
        self._health_monitor = health_monitor
        self._shutdown_event = shutdown_event
        self._pid_file = pid_file
        self._services = services
        self._token: str = config.telegram_token
        self._chat_id: str = config.telegram_chat_id
        self._timeout: int = config.telegram_timeout
        self._last_update_id: int = self._load_update_id()
        self._start_time: float = time.time()
        self._running: bool = True
        self._test_mode: bool = test_mode
        self._shutdown_pending: bool = False
        self._shutdown_request_time: float = 0.0

        self._test_index: int = 0
        self._consecutive_errors: int = 0

        # Link health tracking — never fatal, always recoverable.
        self._link_status: str = "OK"
        self._last_ok_ts: float = time.time()
        self._last_health_write: float = 0.0
        self._last_written_status: Optional[str] = None

        # New modular command system
        from telegram.command_center import CommandCenter  # noqa: PLC0415
        self._command_center = CommandCenter(config, logger=_log,
                                             services=services)

        # Legacy handler dict → now routes through CommandCenter
        self._command_handlers: dict[str, Any] = {}

        _log("Telegram Command Center started (modular dispatch).")
        if self._test_mode:
            _log(f"TEST MODE — simulating {len(_TEST_COMMANDS)} commands")
        else:
            _log(f"Listening for commands on chat {self._chat_id}")

    # ------------------------------------------------------------------
    #  Update ID persistence (prevents replay of old commands after restart)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_update_id() -> int:
        try:
            with open(UPDATE_ID_FILE) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_update_id(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(UPDATE_ID_FILE, "w") as f:
                f.write(str(self._last_update_id))
        except OSError:
            pass

    # ------------------------------------------------------------------
    #  Public
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the polling loop (blocks forever).

        Telegram is a communication channel only — the loop must survive
        any connectivity problem.  Transport timeouts, connection errors
        and temporary API failures trigger an exponential backoff (5s,
        10s, 30s, 60s) and are never treated as fatal.  Only an explicit
        shutdown stops the loop.
        """
        try:
            while self._running and not self._is_shutdown():
                try:
                    updates = self._poll()
                except requests.Timeout as exc:
                    self._record_failure(f"timeout: {exc}")
                    continue
                except requests.ConnectionError as exc:
                    self._record_failure(f"connection lost: {exc}")
                    continue
                except TelegramAPIError as exc:
                    self._record_failure(f"api error: {exc}")
                    continue
                except requests.RequestException as exc:
                    self._record_failure(f"request error: {exc}")
                    continue
                except Exception as exc:
                    self._record_failure(f"unexpected: {exc}")
                    continue

                self._record_success()
                for update in updates:
                    if not self._running or self._is_shutdown():
                        return
                    self._process_update(update)
        except KeyboardInterrupt:
            _log("Shutting down")
            self._running = False

    def _is_shutdown(self) -> bool:
        return (self._shutdown_event is not None
                and self._shutdown_event.is_set())

    def stop(self) -> None:
        """Signal the polling loop to exit."""
        self._running = False

    # ------------------------------------------------------------------
    #  Backoff / link health
    # ------------------------------------------------------------------

    @property
    def link_status(self) -> str:
        """Current link health: OK, DEGRADED or OFFLINE."""
        return self._link_status

    def health_status(self) -> dict[str, Any]:
        """Machine-readable link health snapshot (for health monitor)."""
        return {
            "status": self._link_status,
            "consecutive_errors": self._consecutive_errors,
            "last_ok_ts": self._last_ok_ts,
        }

    def _retry_delay(self) -> float:
        """Exponential backoff schedule: 5s, 10s, 30s, then capped at 60s."""
        idx = max(0, min(self._consecutive_errors - 1, len(RETRY_DELAYS) - 1))
        return float(RETRY_DELAYS[idx])

    def _record_failure(self, reason: str) -> None:
        """Handle a failed poll: back off and degrade, never stop the loop."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= OFFLINE_ERRORS:
            self._set_status("OFFLINE")
        elif self._consecutive_errors >= DEGRADED_ERRORS:
            self._set_status("DEGRADED")
        delay = self._retry_delay()
        _log(f"connection lost, retry in {delay:.0f}s ({reason})")
        self._update_health_file()
        if self._shutdown_event is not None:
            if self._shutdown_event.wait(timeout=delay):
                self._running = False
        else:
            time.sleep(delay)

    def _record_success(self) -> None:
        """Handle a successful poll: reset backoff and restore the link."""
        if self._consecutive_errors > 0:
            _log("connection restored")
        self._consecutive_errors = 0
        self._last_ok_ts = time.time()
        self._set_status("OK")
        self._update_health_file()

    def _set_status(self, status: str) -> None:
        if status == self._link_status:
            return
        _log(f"status → {status}")
        self._link_status = status

    def _update_health_file(self) -> None:
        """Persist link health so the HealthMonitor can report it.

        Writes are throttled — the file is only refreshed when the status
        changes or when HEALTH_WRITE_INTERVAL has elapsed, so a healthy
        polling loop does not touch disk on every iteration.
        """
        now = time.time()
        if (self._last_written_status is not None
                and self._link_status == self._last_written_status
                and now - self._last_health_write < HEALTH_WRITE_INTERVAL):
            return
        self._last_written_status = self._link_status
        self._last_health_write = now
        payload: dict[str, Any] = {
            "status": self._link_status,
            "consecutive_errors": self._consecutive_errors,
            "last_ok_ts": self._last_ok_ts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(TELEGRAM_STATUS_FILE, "w") as f:
                json.dump(payload, f)
        except OSError:
            pass

    # ------------------------------------------------------------------
    #  Polling
    # ------------------------------------------------------------------

    def _poll(self) -> list[dict[str, Any]]:
        if self._test_mode:
            return self._poll_test()
        return self._poll_telegram()

    def _poll_test(self) -> list[dict[str, Any]]:
        """Return canned updates in test mode."""
        time.sleep(1.5)
        if self._test_index >= len(_TEST_COMMANDS):
            _log("Test sequence complete — restarting")
            self._test_index = 0
            return []

        cmd = _TEST_COMMANDS[self._test_index]
        self._test_index += 1
        self._last_update_id += 1
        return [
            {
                "update_id": self._last_update_id,
                "message": {
                    "message_id": self._last_update_id,
                    "chat": {"id": int(self._chat_id) if self._chat_id else 0},
                    "text": cmd,
                    "date": int(time.time()),
                },
            }
        ]

    def _poll_telegram(self) -> list[dict[str, Any]]:
        """Get updates from Telegram via long-polling.

        On failure this raises (``requests.Timeout``,
        ``requests.ConnectionError``, ``TelegramAPIError``) so the caller
        can back off and retry.  An empty ``result`` (no new messages) is
        a normal healthy response and returns ``[]``.
        """
        url = API_BASE.format(token=self._token, method="getUpdates")
        params: dict[str, Any] = {
            "offset": self._last_update_id + 1,
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message"],
        }
        resp = requests.get(url, params=params, timeout=POLL_REQUEST_TIMEOUT)
        resp.raise_for_status()

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(
                f"Telegram returned invalid JSON: {resp.text[:300]}"
            ) from exc

        if not data.get("ok"):
            desc = data.get("description", "no description")
            raise TelegramAPIError(f"Telegram API error (getUpdates): {desc}")

        return data.get("result", [])

    def _process_update(self, update: dict[str, Any]) -> None:
        if not self._running:
            return

        update_id = update.get("update_id", 0)
        if update_id > self._last_update_id:
            self._last_update_id = update_id
            self._save_update_id()

        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()

        # Every real command is typed as "/something" — EXCEPT the one
        # deliberate exception: "CONFIRM LIVE" is a plain-text reply (by
        # design, see telegram/commands/live.py) that must reach
        # ConfirmLiveCommand. Without this exception it was silently
        # dropped right here, before ever reaching the command registry —
        # /golive would tell the operator to reply "CONFIRM LIVE" and the
        # bot would then never respond to it at all.
        is_confirm_live = text.strip().upper() == "CONFIRM LIVE"
        if not text or not (text.startswith("/") or is_confirm_live):
            return

        # Authenticate chat before logging the command
        if not self._test_mode and str(chat_id) != self._chat_id:
            _log(f"Ignored message from unauthorized chat {chat_id}")
            return

        command = text.split()[0].lower()
        _log(f"Received: {command}")

        response = self._command_center.dispatch(
            chat_id=str(chat_id) if chat_id else "",
            message_id=message.get("message_id", 0),
            update_id=update_id,
            text=text,
            exchange=None,
            shutdown_event=self._shutdown_event,
            pid_file=self._pid_file,
            start_time=self._start_time,
            health_monitor=self._health_monitor,
            services=self._services,
        )

        if response is not None:
            if self._running and not self._is_shutdown():
                self._send(response, parse_mode="Markdown")

    # ------------------------------------------------------------------
    #  Send
    # ------------------------------------------------------------------

    def _send(self, text: str, parse_mode: Optional[str] = "Markdown") -> bool:
        if self._test_mode:
            _log(f"Replied successfully ({len(text)} chars)")
            return True

        url = API_BASE.format(token=self._token, method="sendMessage")
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        for attempt in range(1, self._config.telegram_retry + 1):
            if self._is_shutdown():
                return False
            try:
                resp = requests.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                _log("Replied successfully")
                return True
            except requests.ConnectionError as exc:
                _log(
                    f"Telegram connection error (attempt {attempt}/"
                    f"{self._config.telegram_retry}): {exc}"
                )
            except requests.RequestException as exc:
                # Telegram rejects Markdown text with unescaped special
                # characters (400 "can't parse entities").  Resend the
                # same text without parse_mode so the reply always
                # arrives instead of being lost.
                resp = getattr(exc, "response", None)
                if (
                    parse_mode
                    and resp is not None
                    and resp.status_code == 400
                    and "parse" in (getattr(resp, "text", "") or "").lower()
                ):
                    _log("Markdown rejected — resending as plain text")
                    payload.pop("parse_mode", None)
                    parse_mode = ""
                resp_body = (
                    resp.text[:300]
                    if resp is not None
                    else "no response"
                )
                _log(
                    f"Telegram API error (attempt {attempt}/"
                    f"{self._config.telegram_retry}): {exc}"
                    f"  body={resp_body}"
                )
            if attempt < self._config.telegram_retry:
                if self._shutdown_event is not None:
                    if self._shutdown_event.wait(timeout=1 * attempt):
                        return False
                else:
                    time.sleep(1 * attempt)

        _log("Send failed after all retries")
        return False

    # ------------------------------------------------------------------
    #  Legacy command handlers — backward compat, delegate to modular system
    # ------------------------------------------------------------------

    def _cmd_status(self, _text: str) -> str:
        return self._delegate("status")

    def _cmd_pipeline(self, _text: str) -> str:
        return self._delegate("pipeline")

    def _cmd_scan(self, _text: str) -> str:
        return self._delegate("scan")

    def _cmd_positions(self, _text: str) -> str:
        return self._delegate("positions")

    def _cmd_balance(self, _text: str) -> str:
        return self._delegate("balance")

    def _cmd_summary(self, _text: str) -> str:
        return self._delegate("summary")

    def _cmd_health(self, _text: str) -> str:
        return self._delegate("health")

    def _cmd_version(self, _text: str) -> str:
        return self._delegate("version")

    def _cmd_shutdown(self, _text: str) -> str:
        """Shut down the bot gracefully (legacy — direct impl for backward compat)."""
        now = time.time()

        if not self._shutdown_pending or (now - self._shutdown_request_time > 60.0):
            self._shutdown_pending = True
            self._shutdown_request_time = now
            _log("Shutdown confirmation requested")
            return (
                "\u26a0\ufe0f *Shutdown Confirmation*\n"
                "Are you sure? Send /shutdown again within 60 seconds "
                "to confirm and shut down the bot."
            )

        _log("Shutdown confirmed — initiating graceful shutdown")
        # Always write the shutdown signal file so the watchdog sees a
        # deliberate stop and does not auto-restart (BUG A fix).
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SHUTDOWN_FILE, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())

        if self._shutdown_event:
            self._shutdown_event.set()
        self._running = False
        return (
            "\U0001f6d1 *Shutting Down*\n"
            "Graceful shutdown initiated. Goodbye."
        )

    def _cmd_pause(self, _text: str) -> str:
        return self._delegate("pause")

    def _cmd_resume(self, _text: str) -> str:
        return self._delegate("resume")

    def _cmd_help(self, _text: str) -> str:
        return self._delegate("help")

    def _delegate(self, cmd: str) -> str:
        """Dispatch to the modular command system."""
        response = self._command_center.dispatch(
            chat_id=self._chat_id or "",
            message_id=0,
            update_id=0,
            text=f"/{cmd}",
            exchange=None,
            shutdown_event=self._shutdown_event,
            pid_file=self._pid_file,
            start_time=self._start_time,
            health_monitor=self._health_monitor,
            test_mode=self._test_mode,
            services=self._services,
        )
        return response or "Command returned no output."


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    test_mode = os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes")

    config = load_config()

    if not test_mode:
        if not config.telegram_enabled:
            _log("Telegram disabled — set TELEGRAM_ENABLED=true in .env")
            return
        if not config.telegram_token:
            _log("TELEGRAM_TOKEN not set in .env — exiting")
            return
        if not config.telegram_chat_id:
            _log("TELEGRAM_CHAT_ID not set in .env — exiting")
            return

    center = TelegramCommandCenter(config, test_mode=test_mode)
    center.run()


if __name__ == "__main__":
    main()
