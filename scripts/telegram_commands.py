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

MAX_BACKOFF = 120   # maximum sleep between poll retries (seconds)
BASE_DELAY = 2.0    # initial retry delay

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
        """Start the polling loop (blocks forever)."""
        try:
            while self._running:
                try:
                    updates = self._poll()
                    self._consecutive_errors = 0
                    for update in updates:
                        self._process_update(update)
                except requests.Timeout:
                    pass
                except Exception as exc:
                    self._consecutive_errors += 1
                    delay = self._backoff_delay()
                    _log(
                        f"Poll error ({self._consecutive_errors}x): {exc}"
                        f"  retry in {delay:.0f}s"
                    )
                    time.sleep(delay)
        except KeyboardInterrupt:
            _log("Shutting down")
            self._running = False

    def stop(self) -> None:
        """Signal the polling loop to exit."""
        self._running = False

    # ------------------------------------------------------------------
    #  Backoff
    # ------------------------------------------------------------------

    def _backoff_delay(self) -> float:
        """Compute exponential backoff with jitter."""
        delay = min(MAX_BACKOFF, BASE_DELAY * (2 ** (self._consecutive_errors - 1)))
        # Add ±25% jitter
        import random
        jitter = 1.0 + (random.random() - 0.5) * 0.5
        return round(delay * jitter, 1)

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
        """Get updates from Telegram via long-polling."""
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
            _log(f"Telegram returned invalid JSON: {resp.text[:300]}")
            return []

        if not data.get("ok"):
            desc = data.get("description", "no description")
            _log(f"Telegram API error (getUpdates): {desc}")
            return []

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

        if not text or not text.startswith("/"):
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
            self._send(response)

    # ------------------------------------------------------------------
    #  Send
    # ------------------------------------------------------------------

    def _send(self, text: str) -> bool:
        if self._test_mode:
            _log(f"Replied successfully ({len(text)} chars)")
            return True

        url = API_BASE.format(token=self._token, method="sendMessage")
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, self._config.telegram_retry + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                _log("Replied successfully")
                return True
            except requests.RequestException as exc:
                resp_body = (
                    exc.response.text[:300]
                    if exc.response is not None
                    else "no response"
                )
                _log(
                    f"Telegram API error (attempt {attempt}/"
                    f"{self._config.telegram_retry}): {exc}"
                    f"  body={resp_body}"
                )
                if attempt < self._config.telegram_retry:
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
        if not self._test_mode and now - self._start_time < STARTUP_GRACE_PERIOD:
            _log("Shutdown ignored — still in startup grace period")
            return (
                "\u26a0\ufe0f *Shutdown ignored*\n"
                f"Bot just started. Try again in "
                f"{int(STARTUP_GRACE_PERIOD - (now - self._start_time))}s."
            )

        if not self._shutdown_pending or (now - self._shutdown_request_time > 60.0):
            self._shutdown_pending = True
            self._shutdown_request_time = now
            _log("Shutdown confirmation requested")
            return (
                "\u26a0\ufe0f *Shutdown Confirmation*\n"
                "Are you sure? Send `/shutdown` again within 60 seconds "
                "to confirm and shut down the bot."
            )

        _log("Shutdown confirmed — initiating graceful shutdown")
        if self._shutdown_event:
            self._shutdown_event.set()
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SHUTDOWN_FILE, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
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
