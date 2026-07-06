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
    /help       — List all available commands
"""

import json
import os
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
POLL_TIMEOUT = 30          # long-poll seconds
POLL_REQUEST_TIMEOUT = 35  # HTTP request timeout (must exceed POLL_TIMEOUT)
PAUSE_FILE = "data/.paused"
DATA_DIR = "data"

MAX_BACKOFF = 120   # maximum sleep between poll retries (seconds)
BASE_DELAY = 2.0    # initial retry delay

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
    ) -> None:
        self._config = config
        self._health_monitor = health_monitor
        self._token: str = config.telegram_token
        self._chat_id: str = config.telegram_chat_id
        self._timeout: int = config.telegram_timeout
        self._last_update_id: int = 0
        self._start_time: float = time.time()
        self._running: bool = True
        self._test_mode: bool = test_mode

        self._test_index: int = 0
        self._consecutive_errors: int = 0

        self._command_handlers: dict[str, Any] = {
            "/status":    self._cmd_status,
            "/pipeline":  self._cmd_pipeline,
            "/scan":      self._cmd_scan,
            "/positions": self._cmd_positions,
            "/balance":   self._cmd_balance,
            "/summary":   self._cmd_summary,
            "/health":    self._cmd_health,
            "/pause":     self._cmd_pause,
            "/resume":    self._cmd_resume,
            "/help":      self._cmd_help,
            "/start":     self._cmd_help,
        }

        _log("Telegram Command Center started.")
        if self._test_mode:
            _log(f"TEST MODE — simulating {len(_TEST_COMMANDS)} commands")
        else:
            _log(f"Listening for commands on chat {self._chat_id}")

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
        update_id = update.get("update_id", 0)
        if update_id > self._last_update_id:
            self._last_update_id = update_id

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

        handler = self._command_handlers.get(command)
        if handler is None:
            self._send(
                f"Unknown command `{command}`.  Use /help for available commands.",
            )
            return

        try:
            response = handler(text)
            if response:
                self._send(response)
        except Exception as exc:
            _log(f"Command failed: {command} — {exc}")
            self._send(f"Command `{command}` failed: `{exc}`")

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
    #  Command handlers
    # ------------------------------------------------------------------

    def _cmd_status(self, _text: str) -> str:
        """Bot status, runtime, balance, positions overview."""
        runtime = time.time() - self._start_time
        hours, remainder = divmod(int(runtime), 3600)
        minutes, seconds = divmod(remainder, 60)

        pb = _read_json(f"{DATA_DIR}/paper_balance.json")
        pos_data = _read_json(f"{DATA_DIR}/positions.json")
        pos_list = pos_data.get("positions", [])

        open_pos = sum(
            1 for p in pos_list if p.get("status") == "OPEN"
        )
        closed_pos = sum(
            1 for p in pos_list if p.get("status") in ("CLOSED", "STOPPED", "TIMEOUT")
        )
        paused = os.path.exists(PAUSE_FILE)

        return (
            f"\U0001f916 *Bot Status*\n"
            f"Status: `ONLINE`\n"
            f"Exchange: `{self._config.exchange}`\n"
            f"Mode: `{'PAPER' if self._config.paper_mode else 'LIVE'}`\n"
            f"Runtime: `{hours:02d}:{minutes:02d}:{seconds:02d}`\n"
            f"Trading: `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`\n"
            f"Balance: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Equity: `${pb.get('final_equity', 0):,.2f}`\n"
            f"Net PnL: `${pb.get('net_pnl', 0):+,.2f}`\n"
            f"Open Positions: `{open_pos}`\n"
            f"Closed Positions: `{closed_pos}`"
        )

    def _cmd_pipeline(self, _text: str) -> str:
        """Run full pipeline (scanner → decision → risk → trade → position → paper)."""
        self._send(
            "\U0001f4ca Running pipeline... "
            f"(this takes ~{int(self._config.scanner_threads * 12)}s)"
        )

        from scripts.logger import PipelineLogger
        from scripts.pipeline import Pipeline

        logger = PipelineLogger(self._config)
        pipeline = Pipeline(self._config, logger)
        results = pipeline.run()

        total = sum(r.duration for r in results)
        lines: list[str] = ["\U0001f4ca *Pipeline Report*"]
        for r in results:
            icon = "\u2705" if r.success else "\u274c"
            detail = f"  {r.detail}" if r.detail else ""
            lines.append(f"{icon} `{r.name:>10s}` {r.duration:.1f}s{detail}")
        lines.append(f"Total: `{total:.1f}s`")

        failed = [r for r in results if not r.success]
        if failed:
            lines.append("")
            lines.append(f"\u26a0\ufe0f *{len(failed)} stage(s) failed*")
            for f in failed:
                lines.append(f"`{f.name}`: {f.error}")

        return "\n".join(lines)

    def _cmd_scan(self, _text: str) -> str:
        """Run scanner only."""
        self._send("Scanning market... (this takes ~60s)")

        from scripts import scanner
        scanner.main()

        data = _read_json(f"{DATA_DIR}/scanner_results.json")
        total = data.get("total_pairs", 0)
        results_list = data.get("results", data.get("sorted", []))
        top_count = min(
            5, len(results_list) if isinstance(results_list, list) else 0
        )
        top_pairs: list[str] = []
        if isinstance(results_list, list):
            for i, s in enumerate(results_list[:top_count], 1):
                sym = s.get("symbol", "?")
                score = s.get("overall_score", 0)
                top_pairs.append(f"  `{i}. {sym}` score={score:.1f}")

        lines = [
            f"\U0001f50d *Scan Complete*",
            f"Pairs scanned: `{total}`",
        ]
        if top_pairs:
            lines.append("")
            lines.append("Top pairs:")
            lines.extend(top_pairs)

        return "\n".join(lines)

    def _cmd_positions(self, _text: str) -> str:
        """List all open positions with entry, PnL, SL, TP levels."""
        pos_data = _read_json(f"{DATA_DIR}/positions.json")
        pos_list = pos_data.get("positions", [])

        open_positions = [p for p in pos_list if p.get("status") == "OPEN"]

        if not open_positions:
            return "No open positions."

        chunks: list[str] = []
        for p in open_positions:
            pnl = p.get("floating_pnl", 0)
            pnl_pct = p.get("floating_pnl_pct", 0)
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

            chunk = (
                f"{emoji} *{p['symbol']}*\n"
                f"Entry: `{p['entry_price']:.6f}`  "
                f"Curr: `{p['current_price']:.6f}`\n"
                f"PnL: `${pnl:+,.2f}` ({pnl_pct:+.2f}%)\n"
                f"SL: `{p.get('stop_loss', 0):.6f}`\n"
                f"TP1: `{p.get('tp1', 0):.6f}`  "
                f"TP2: `{p.get('tp2', 0):.6f}`  "
                f"TP3: `{p.get('tp3', 0):.6f}`\n"
                f"Holding: `{p.get('holding_hours', 0):.1f}h`  "
                f"Size: `${p.get('position_size_usdt', 0):,.2f}`"
            )
            chunks.append(chunk)

        return "\n\n".join(chunks)

    def _cmd_balance(self, _text: str) -> str:
        """Account balance, equity, PnL."""
        pb = _read_json(f"{DATA_DIR}/paper_balance.json")
        if not pb:
            return "No balance data yet.  Run `/pipeline` first."

        return (
            f"\U0001f4b0 *Balance*\n"
            f"Free USDT: `${pb.get('final_balance', 0):,.2f}`\n"
            f"Equity: `${pb.get('final_equity', 0):,.2f}`\n"
            f"Realized PnL: `${pb.get('realized_pnl', 0):+,.2f}`\n"
            f"Unrealized PnL: `${pb.get('unrealized_pnl', 0):+,.2f}`\n"
            f"Net PnL: `${pb.get('net_pnl', 0):+,.2f}`\n"
            f"Return: `{pb.get('total_return_pct', 0):+.2f}%`"
        )

    def _cmd_summary(self, _text: str) -> str:
        """Today's trading statistics."""
        pb = _read_json(f"{DATA_DIR}/paper_balance.json")
        orders_data = _read_json(f"{DATA_DIR}/paper_orders.json")

        total_trades = pb.get("total_trades", 0)

        if total_trades == 0:
            return "No completed trades yet."

        closed_orders = [
            o for o in orders_data.get("orders", [])
            if o.get("status") == "CLOSED"
        ]

        best: Optional[dict[str, Any]] = None
        worst: Optional[dict[str, Any]] = None
        if closed_orders:
            best = max(closed_orders, key=lambda o: o.get("net_pnl", 0))
            worst = min(closed_orders, key=lambda o: o.get("net_pnl", 0))

        lines = [
            f"\U0001f4ca *Trading Summary*",
            f"Total Trades: `{total_trades}`",
            f"Wins: `{pb.get('winning_trades', 0)}`  "
            f"Losses: `{pb.get('losing_trades', 0)}`",
            f"Win Rate: `{pb.get('win_rate', 0):.1f}%`",
            f"Profit Factor: `{pb.get('profit_factor', 0):.2f}`",
            f"Gross Profit: `${pb.get('gross_profit', 0):,.2f}`",
            f"Gross Loss: `${pb.get('gross_loss', 0):,.2f}`",
            f"Net Profit: `${pb.get('net_pnl', 0):+,.2f}`",
        ]

        if best:
            lines.append(
                f"Best Trade: `{best['symbol']}`  "
                f"`${best['net_pnl']:+,.2f}`"
            )
        if worst:
            lines.append(
                f"Worst Trade: `{worst['symbol']}`  "
                f"`${worst['net_pnl']:+,.2f}`"
            )

        return "\n".join(lines)

    def _cmd_health(self, _text: str) -> str:
        """System health overview using HealthMonitor snapshot (realtime)."""
        snapshot: dict[str, Any] = {}
        if self._health_monitor:
            try:
                snapshot = self._health_monitor.force_refresh()
            except Exception as exc:
                _log(f"HealthMonitor snapshot failed: {exc}")

        ver = snapshot.get("version", "?")
        uptime_sec = snapshot.get("uptime_sec", 0)
        rss_kb = snapshot.get("rss_kb", 0)
        thread_count = snapshot.get("thread_count", 0)
        process_cpu_sec = snapshot.get("process_cpu_sec", 0)
        internet_ok = snapshot.get("internet_ok", False)
        exchange_ok = snapshot.get("exchange_ok", False)
        scanner_time = snapshot.get("scanner_time", "N/A")
        api_time = snapshot.get("api_time", "N/A")
        balance = snapshot.get("balance", 0.0)
        equity = snapshot.get("equity", 0.0)
        net_pnl = snapshot.get("net_pnl", 0.0)
        open_positions = snapshot.get("open_positions", 0)
        total_trades = snapshot.get("total_trades", 0)
        win_rate = snapshot.get("win_rate", 0.0)
        paused = snapshot.get("paused", False)
        paper_mode = snapshot.get("paper_mode", True)

        hours, rem = divmod(int(uptime_sec), 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        cpu_pct = (process_cpu_sec / uptime_sec * 100) if uptime_sec > 0 else 0.0
        rss_mb = rss_kb / 1024.0

        # Component icons
        def _icon(status: bool, label_ok: str = "Healthy") -> tuple[str, str]:
            if status:
                return "\U0001f7e2", label_ok
            return "\U0001f534", "Critical"

        internet_icon, internet_status = _icon(internet_ok, "Connected" if internet_ok else "Disconnected")
        exchange_icon, exchange_status = _icon(exchange_ok, "Connected")

        # Scanner health based on data freshness
        scanner_age = snapshot.get("scanner_age", float("inf"))
        if scanner_age == float("inf"):
            scanner_icon, scanner_status = "\U0001f534", "No Data"
        elif scanner_age < 7200:
            scanner_icon, scanner_status = "\U0001f7e2", "Healthy"
        elif scanner_age < 86400:
            scanner_icon, scanner_status = "\U0001f7e1", "Stale"
        else:
            scanner_icon, scanner_status = "\U0001f534", "Critical"

        # Telegram: check if telegram thread is alive
        has_creds = bool(self._config.telegram_token and self._config.telegram_chat_id)
        tg_alive = False
        if has_creds:
            for t in threading.enumerate():
                if t.name == "TelegramCmd" and t.is_alive():
                    tg_alive = True
                    break
        if has_creds and tg_alive:
            telegram_icon, telegram_status = "\U0001f7e2", "Healthy"
        elif has_creds:
            telegram_icon, telegram_status = "\U0001f534", "Not Running"
        else:
            telegram_icon, telegram_status = "\u26aa", "Disabled"

        # Resource thresholds
        cpu_icon = "\U0001f7e2" if cpu_pct < 80 else ("\U0001f7e1" if cpu_pct < 95 else "\U0001f534")
        cpu_label = "Healthy" if cpu_pct < 80 else ("Warning" if cpu_pct < 95 else "Critical")
        mem_icon = "\U0001f7e2" if rss_mb < 200 else ("\U0001f7e1" if rss_mb < 500 else "\U0001f534")
        mem_label = "Healthy" if rss_mb < 200 else ("Warning" if rss_mb < 500 else "Critical")
        thr_icon = "\U0001f7e2" if thread_count < 30 else ("\U0001f7e1" if thread_count < 50 else "\U0001f534")
        thr_label = "Healthy" if thread_count < 30 else ("Warning" if thread_count < 50 else "Critical")

        # Health Score from real component state
        score = 100
        if not internet_ok:
            score -= 20
        if not exchange_ok:
            score -= 20
        if scanner_status == "Critical":
            score -= 15
        elif scanner_status == "Stale":
            score -= 5
        if telegram_status == "Not Running":
            score -= 15
        elif telegram_status == "Disabled":
            score -= 0
        if cpu_label == "Critical":
            score -= 10
        elif cpu_label == "Warning":
            score -= 5
        if mem_label == "Critical":
            score -= 10
        elif mem_label == "Warning":
            score -= 5
        if thr_label == "Critical":
            score -= 5
        elif thr_label == "Warning":
            score -= 3
        score = max(0, min(100, score))
        score_icon = "\U0001f7e2" if score >= 80 else ("\U0001f7e1" if score >= 50 else "\U0001f534")

        return (
            f"{score_icon} *ZetBot {ver} Health*\n\n"
            f"*Score:* `{score}/100`\n\n"
            f"*System*\n"
            f"Uptime:     `{uptime_str}`\n"
            f"Mode:       `{'PAPER' if paper_mode else 'LIVE'}`\n"
            f"Trading:    `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`\n\n"
            f"*Resources*\n"
            f"CPU:        `{cpu_pct:.1f}%`  {cpu_icon} {cpu_label}\n"
            f"Memory:     `{rss_mb:.1f}MB`  {mem_icon} {mem_label}\n"
            f"Threads:    `{thread_count}`  {thr_icon} {thr_label}\n\n"
            f"*Components*\n"
            f"Internet:   {internet_icon} {internet_status}\n"
            f"Exchange:   {exchange_icon} {exchange_status}\n"
            f"Scanner:    {scanner_icon} {scanner_status}\n"
            f"Telegram:   {telegram_icon} {telegram_status}\n\n"
            f"*Account*\n"
            f"Equity:     `${equity:,.2f}`\n"
            f"Balance:    `${balance:,.2f}`\n"
            f"Net PnL:    `${net_pnl:+,.2f}`\n"
            f"Win Rate:   `{win_rate:.1f}%`\n\n"
            f"*Positions*\n"
            f"Open:       `{open_positions}`\n"
            f"Total:      `{total_trades}`\n\n"
            f"*Timestamps*\n"
            f"Last Scan:  `{scanner_time}`\n"
            f"Last Trade: `{api_time}`"
        )

    def _cmd_pause(self, _text: str) -> str:
        """Disable new trade openings."""
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PAUSE_FILE, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        _log("Trading paused")
        return "\u23f8\ufe0f *Trading Paused*\nNew trades will not be opened."

    def _cmd_resume(self, _text: str) -> str:
        """Enable new trade openings."""
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        _log("Trading resumed")
        return "\u25b6\ufe0f *Trading Resumed*\nNew trade openings enabled."

    def _cmd_help(self, _text: str) -> str:
        """List all available commands."""
        return (
            "\U0001f4ac *ZetBot Commands*\n\n"
            "/status \u2014 Bot status, balance, positions\n"
            "/pipeline \u2014 Run full analysis pipeline\n"
            "/scan \u2014 Run market scanner only\n"
            "/positions \u2014 Show open positions\n"
            "/balance \u2014 Account balance & equity\n"
            "/summary \u2014 Today's trading statistics\n"
            "/pause \u2014 Disable new trades\n"
            "/resume \u2014 Enable new trades\n"
            "/health \u2014 System health & status overview\n"
            "/help \u2014 Show this message"
        )


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
