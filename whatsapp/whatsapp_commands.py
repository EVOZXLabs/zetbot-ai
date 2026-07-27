"""WhatsApp Command Center for ZetBot AI, via Twilio's WhatsApp API.

Unlike Telegram (long-polling), WhatsApp delivers messages by HTTP
webhook — Twilio POSTs each inbound message to a URL you control. This
module runs a small Flask app for that webhook and dispatches through
the *same* ``telegram.command_center.CommandCenter`` / command classes
Telegram uses, so ``/status``, ``/balance``, ``/wallet`` etc. all work
identically over WhatsApp with zero duplicated command logic.

Setup (see docs/SETUP_WEB3_WHATSAPP.md for the full walkthrough)::

    WHATSAPP_ENABLED=true
    TWILIO_ACCOUNT_SID=...
    TWILIO_AUTH_TOKEN=...
    TWILIO_WHATSAPP_FROM=whatsapp:+14155238886   # Twilio's sandbox or your approved sender
    WHATSAPP_ALLOWED_NUMBERS=whatsapp:+628123456789   # comma-separated allow-list

    python -m whatsapp.whatsapp_commands

TEST_MODE (no Twilio credentials required, exercises dispatch only)::

    TEST_MODE=true python -m whatsapp.whatsapp_commands

Security note: the allow-list is mandatory. Twilio's webhook URL is
public by nature (Twilio needs to reach it), so without an allow-list
*anyone* who discovers the URL could message your bot and try commands.
If ``WHATSAPP_ALLOWED_NUMBERS`` is empty, the webhook refuses all
messages rather than defaulting to "allow everyone".
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from scripts.app_config import AppConfig

# WhatsApp messages are capped at 1600 characters by Twilio/WhatsApp.
WHATSAPP_MAX_CHARS = 1550


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [WA] {msg}", flush=True)


def _to_whatsapp_formatting(text: str) -> str:
    """Best-effort conversion of the bot's Telegram-Markdown output to
    WhatsApp's formatting syntax.

    Both use ``*bold*`` and ``_italic_`` identically, so those pass
    through untouched. WhatsApp supports triple-backtick fenced blocks
    (kept verbatim below) but has no single-backtick inline-code span,
    so a lone `` ` `` around a short token (e.g. a progress bar) is
    dropped rather than left as a stray visible character.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_fence = False
    while i < n:
        if text[i:i + 3] == "```":
            out.append("```")
            in_fence = not in_fence
            i += 3
            continue
        if text[i] == "`" and not in_fence:
            i += 1  # drop lone backtick
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _truncate(text: str) -> str:
    if len(text) <= WHATSAPP_MAX_CHARS:
        return text
    return text[: WHATSAPP_MAX_CHARS - 20].rstrip() + "\n… (truncated)"


class WhatsAppCommandCenter:
    """Receives Twilio WhatsApp webhooks and dispatches to CommandCenter."""

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
        self._test_mode = test_mode
        self._start_time = time.time()

        self._account_sid = config.twilio_account_sid
        self._auth_token = config.twilio_auth_token
        self._from_number = config.twilio_whatsapp_from
        self._allowed = {
            n.strip() for n in (config.whatsapp_allowed_numbers or "").split(",") if n.strip()
        }

        from telegram.command_center import CommandCenter  # noqa: PLC0415
        from telegram import permissions  # noqa: PLC0415

        self._command_center = CommandCenter(config, logger=_log, services=services)
        # CommandCenter.__init__ already registered Telegram's chat id;
        # register every allowed WhatsApp number too (additive, see
        # telegram/permissions.py — configure() no longer overwrites).
        for number in self._allowed:
            permissions.configure(number)

        if not self._allowed:
            _log(
                "WARNING: WHATSAPP_ALLOWED_NUMBERS is empty — the webhook "
                "will reject every inbound message until it's set."
            )

        self._app = self._build_flask_app()
        _log("WhatsApp Command Center initialized (modular dispatch).")

    # ------------------------------------------------------------------
    #  Flask app
    # ------------------------------------------------------------------

    def _build_flask_app(self) -> Any:
        from flask import Flask, request, Response  # noqa: PLC0415

        app = Flask(__name__)

        @app.route("/whatsapp/webhook", methods=["POST"])
        def webhook() -> Any:  # noqa: ANN401
            from_number = request.form.get("From", "")
            body = (request.form.get("Body") or "").strip()

            if from_number not in self._allowed:
                _log(f"Rejected message from unauthorized number {from_number}")
                return Response(status=204)

            if not body:
                return Response(status=204)

            reply = self._dispatch(from_number, body)
            twiml = self._build_twiml(reply) if reply else "<Response></Response>"
            return Response(twiml, mimetype="application/xml")

        @app.route("/whatsapp/health", methods=["GET"])
        def health() -> Any:  # noqa: ANN401
            return {"status": "ok", "channel": "whatsapp"}

        return app

    @staticmethod
    def _build_twiml(text: str) -> str:
        from xml.sax.saxutils import escape

        formatted = _truncate(_to_whatsapp_formatting(text))
        return f"<Response><Message>{escape(formatted)}</Message></Response>"

    # ------------------------------------------------------------------
    #  Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, from_number: str, text: str) -> Optional[str]:
        _log(f"Received from {from_number}: {text[:60]!r}")
        try:
            return self._command_center.dispatch(
                chat_id=from_number,
                message_id=0,
                update_id=int(time.time() * 1000),
                text=text,
                exchange=None,
                shutdown_event=self._shutdown_event,
                pid_file=self._pid_file,
                start_time=self._start_time,
                health_monitor=self._health_monitor,
                services=self._services,
                test_mode=self._test_mode,
            )
        except Exception as exc:
            _log(f"Dispatch error: {exc}")
            return f"⚠️ Error handling command: {exc}"

    # ------------------------------------------------------------------
    #  Outbound (proactive) messages — trade alerts, etc.
    # ------------------------------------------------------------------

    def send(self, text: str) -> bool:
        """Send a message the bot initiated (not a reply to an inbound
        message) — e.g. a trade notification. Uses the Twilio REST API
        directly, since TwiML replies only work within a webhook
        response. Only sends to numbers in the allow-list.
        """
        if self._test_mode:
            _log(f"[TEST MODE] Would send: {text[:60]!r}")
            return True
        if not (self._account_sid and self._auth_token and self._from_number):
            _log("Cannot send — Twilio credentials not fully configured")
            return False

        formatted = _truncate(_to_whatsapp_formatting(text))
        ok = True
        for number in self._allowed:
            try:
                resp = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json",
                    auth=(self._account_sid, self._auth_token),
                    data={"From": self._from_number, "To": number, "Body": formatted},
                    timeout=15,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                _log(f"Send to {number} failed: {exc}")
                ok = False
        return ok

    # ------------------------------------------------------------------
    #  Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the webhook server (blocks forever)."""
        host = self._config.whatsapp_webhook_host
        port = self._config.whatsapp_webhook_port
        _log(f"Listening for WhatsApp webhooks on {host}:{port}/whatsapp/webhook")
        # Flask's built-in server is fine for the sandbox/small-scale case
        # this ships with; for production, run behind gunicorn + a
        # reverse proxy with a real TLS certificate instead (Twilio
        # requires HTTPS for the webhook URL).
        self._app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    from scripts.app_config import load_config

    cfg = load_config()
    test_mode = os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes")
    center = WhatsAppCommandCenter(cfg, test_mode=test_mode)
    center.run()
