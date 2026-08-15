#!/usr/bin/env python3
"""
ZetBot AI — Application Orchestrator (v0.5.0)

Production-hardened long-running daemon.  Integrates the trading
analysis pipeline with the Telegram Command Center in a single
process.  Includes PID lock, health monitor, thread watchdog, and
graceful shutdown.

Usage::

    python main.py

All configuration is read from environment variables / .env.
See ``scripts.app_config`` for the full list of options.
"""

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
#  Early shutdown coordination — install signal handlers BEFORE slow module
#  imports (ccxt can take 5-7 seconds on Python 3.14) so that SIGTERM/SIGINT
#  are always handled, even during import phase.
# ---------------------------------------------------------------------------
_early_shutdown_event = threading.Event()


def _early_shutdown_handler(signum: int, _frame: Any) -> None:
    """Handle SIGTERM/SIGINT during or after startup."""
    _early_shutdown_event.set()
    threading.Timer(8.0, os._exit, args=[0]).start()


signal.signal(signal.SIGINT, _early_shutdown_handler)
signal.signal(signal.SIGTERM, _early_shutdown_handler)

# Capture launch time BEFORE slow module-level imports (ccxt can take
# 5-7 s on Python 3.14) so we can distinguish genuinely stale shutdown
# signal files (from a previous run) from files created during our own
# import phase (e.g. by a test or external tool).
_PROCESS_LAUNCH_TIME: float = time.time()

# Watchdog heartbeat cadence.  The dedicated heartbeat thread writes
# data/watchdog_heartbeat.json every HEARTBEAT_INTERVAL_SECONDS while the
# process runs.  The watchdog (scripts/watchdog.py) restarts the bot when
# that file goes stale (default HEARTBEAT_STALE_SECONDS=300), so the write
# interval must stay far below the staleness threshold.  The heartbeat
# intentionally does NOT depend on position monitoring: monitoring can be
# slow, raise, or hang on an exchange call without the bot being dead.
HEARTBEAT_INTERVAL_SECONDS: float = max(
    1.0, float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "30"))
)

from scripts.app_config import (
    AppConfig,
    ConfigError,
    load_config,
    validate_config,
)
from scripts.service_container import ServiceContainer
from scripts.health import HealthMonitor
from scripts.logger import PipelineLogger
from scripts.pidfile import PidFile
from scripts.pipeline import Pipeline, StageResult
from scripts.position_status import is_open, OPEN_STATUSES, CLOSED_STATUSES
from scripts.paper_state_lock import paper_state_writes, merge_positions

# ---------------------------------------------------------------------------
#  Configure the ZetBot logger — bot/notifier.py and all bot/ modules use
#  logging.getLogger("ZetBot") which needs a handler.  bot/logger.py
#  configures basicConfig with a console + file handler.  Importing it here
#  ensures the logger works throughout the process.
# ---------------------------------------------------------------------------
import bot.logger  # noqa: E402, F401


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _read_json(path: str) -> dict[str, Any]:
    """Read a JSON file, returning an empty dict on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _build_summary(
    results: list[StageResult],
    config: AppConfig,
) -> list[str]:
    """Build human-readable summary lines from pipeline output files."""
    lines: list[str] = []

    scanner_data = _read_json("data/scanner_results.json")
    decision_data = _read_json("data/decision_results.json")
    risk_data = _read_json("data/risk_results.json")
    trade_data = _read_json("data/trade_plan.json")
    position_data = _read_json("data/positions.json")
    paper_balance = _read_json("data/paper_balance.json")

    # Scanner
    scanned = scanner_data.get("total_pairs", 0)
    results_list = scanner_data.get("results", scanner_data.get("sorted", []))
    buy_candidates = sum(
        1 for s in results_list if isinstance(s, dict) and s.get("overall_score", 0) >= 70
    ) if isinstance(results_list, list) else 0
    lines.append(f"Pairs scanned       : {scanned}")
    lines.append(f"BUY candidates      : {buy_candidates}")

    # Decision engine
    decisions_list = decision_data.get("decisions", [])
    total_analyzed = len(decisions_list)
    strong_buys = sum(
        1 for d in decisions_list if d.get("recommendation") == "STRONG BUY"
    ) if isinstance(decisions_list, list) else 0
    good = sum(
        1 for d in decisions_list if d.get("recommendation") == "GOOD"
    ) if isinstance(decisions_list, list) else 0
    lines.append(f"STRONG BUY signals  : {strong_buys}  (of {total_analyzed} analyzed)")
    if good:
        lines.append(f"GOOD signals        : {good}")

    # Risk manager
    risk_results_list = risk_data.get("results", [])
    total_risk = len(risk_results_list)
    approved = sum(
        1 for r in risk_results_list if r.get("approval") == "APPROVED"
    ) if isinstance(risk_results_list, list) else 0
    rejected = sum(
        1 for r in risk_results_list if r.get("approval") == "REJECTED"
    ) if isinstance(risk_results_list, list) else 0
    lines.append(f"Risk evaluations    : {total_risk}")
    lines.append(f"Approved trades     : {approved}")
    if rejected:
        lines.append(f"Rejected trades     : {rejected}")

    # Trade plan
    ready_plans = trade_data.get("ready_count", 0)
    lines.append(f"Ready plans         : {ready_plans}")

    # Positions
    pos_list = position_data.get("positions", [])
    open_positions = sum(
        1 for p in pos_list if is_open(p.get("status"))
    ) if isinstance(pos_list, list) else 0
    closed_positions = sum(
        1 for p in pos_list if p.get("status") in CLOSED_STATUSES
    ) if isinstance(pos_list, list) else 0
    lines.append(f"Open positions      : {open_positions}")
    lines.append(f"Closed positions    : {closed_positions}")

    # Paper trading — canonical MetricsManager snapshot (same source as
    # HEALTH and the Telegram wallet/portfolio/status commands), never
    # the raw paper_balance.json keys: net_pnl/equity are only refreshed
    # on position closure and sit stale (or absent) between closures,
    # which made the pipeline report disagree with every other view.
    try:
        from scripts.metrics_manager import MetricsManager  # noqa: PLC0415
        snap = MetricsManager(
            config.data_dir,
            mode_provider=lambda: "PAPER" if config.paper_mode else "LIVE",
        ).account()
        balance = snap.balance
        equity = snap.equity
        realized = snap.realized_pnl
        unrealized = snap.unrealized_pnl
        net_pnl = snap.net_pnl
        total_trades = snap.total_trades
        wins = snap.winning_trades
        losses = snap.losing_trades
        win_rate = snap.win_rate
    except Exception:
        balance = paper_balance.get("final_balance", 0.0)
        equity = paper_balance.get("final_equity", 0.0)
        realized = paper_balance.get("realized_pnl", 0.0)
        unrealized = paper_balance.get("unrealized_pnl", 0.0)
        net_pnl = paper_balance.get("net_pnl", 0.0)
        win_rate = paper_balance.get("win_rate", 0.0)
        total_trades = paper_balance.get("total_trades", 0)
        wins = paper_balance.get("winning_trades", 0)
        losses = paper_balance.get("losing_trades", 0)
    lines.append(f"Today's trades      : {total_trades}  (W:{wins} L:{losses})")
    lines.append(f"Win rate            : {win_rate:.1f}%")
    lines.append(f"Realized PnL        : {realized:>+10,.2f} {config.quote_currency}")
    lines.append(f"Unrealized PnL      : {unrealized:>+10,.2f} {config.quote_currency}")
    lines.append(f"Net PnL             : {net_pnl:>+10,.2f} {config.quote_currency}")
    lines.append(f"{config.quote_currency} balance        : {balance:>10,.2f} {config.quote_currency}")
    lines.append(f"Equity              : {equity:>10,.2f} {config.quote_currency}")
    lines.append(f"Cash                : {balance:>10,.2f} {config.quote_currency}")

    # Stage execution times
    lines.append("")
    lines.append("Stage times:")
    for r in results:
        tag = "OK" if r.success else "FAIL"
        lines.append(f"  {r.name:>12s}  {tag}  {r.duration:.1f}s")
    total = sum(r.duration for r in results)
    lines.append(f"  {'TOTAL':>12s}       {total:.1f}s")

    return lines


def _thread_exists(name: str) -> bool:
    """Check if a thread with the given name is alive (zombie detection)."""
    for t in threading.enumerate():
        if t.name == name and t.is_alive():
            return True
    return False


def _start_worker(
    name: str,
    target: Any,
    logger: PipelineLogger,
) -> threading.Thread | None:
    """Start a worker thread if no thread with *name* exists.

    Returns the thread on success, None if a zombie duplicate was found.
    """
    if _thread_exists(name):
        logger.error(
            "[WATCHDOG] Duplicate worker '%s' detected — not starting",
            name,
        )
        return None
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _start_heartbeat_writer(
    shutdown_event: threading.Event,
    logger: Any,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    writer: Any = None,
) -> threading.Thread:
    """Start the watchdog heartbeat writer as a dedicated daemon thread.

    Writes ``data/watchdog_heartbeat.json`` immediately (with the startup
    log line ``[WATCHDOG] heartbeat updated``) and then every ``interval``
    seconds until shutdown.  The heartbeat must NEVER be coupled to
    ``_monitor_positions()``: monitoring can be slow, raise, or hang on an
    exchange call, and none of that means the bot is dead — the Telegram
    command center runs in its own thread and stays responsive.  A separate
    thread keeps the heartbeat fresh for as long as the process runs, so
    the watchdog (which restarts on stale heartbeats) cannot kill a
    healthy bot just because position monitoring misbehaved.
    """
    if writer is None:
        from scripts.pipeline_scheduler import write_heartbeat  # noqa: PLC0415
        writer = write_heartbeat

    def _writer_loop() -> None:
        try:
            writer()
            logger.info("[WATCHDOG] heartbeat updated")
        except Exception as exc:
            logger.warning(f"[WATCHDOG] initial heartbeat write failed: {exc}")
        while not shutdown_event.wait(interval):
            try:
                writer()
            except Exception as exc:
                logger.debug(f"[WATCHDOG] heartbeat write failed: {exc}")

    thread = threading.Thread(
        target=_writer_loop,
        name="HeartbeatWriter",
        daemon=True,
    )
    thread.start()
    return thread


def _send_telegram(notifier: Any, lines: list[str]) -> None:
    """Send summary via Telegram using the centralized Notifier."""
    try:
        from telegram.formatter import md_escape  # noqa: PLC0415
        msg = "\n".join(line for line in lines[:25])
        notifier.send(f"\U0001f4ca *Pipeline Report*\n{md_escape(msg)}")
    except Exception as exc:
        logger.error(f"Telegram notification failed: {exc}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def _check_dependencies() -> None:
    """Verify critical dependencies are importable; exit with helpful message if not."""
    # Skip in test mode to avoid import deadlocks and keep startup fast.
    if os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes"):
        return

    # Use subprocess to avoid import deadlocks between certain packages.
    # Import ccxt first — on Python 3.14 there is an intermittent import
    # lock deadlock when numpy/pandas are loaded before ccxt.
    import subprocess  # noqa: PLC0415

    code = (
        "import sys\n"
        "for mod in (\"ccxt\", \"requests\", \"numpy\", \"pandas\"):\n"
        "    try:\n"
        "        __import__(mod)\n"
        "    except Exception:\n"
        "        print(mod, file=sys.stderr)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("WARNING: Dependency check timed out — continuing anyway", file=sys.stderr)
        return
    except Exception:
        return
    if result.returncode != 0:
        print(f"ERROR: Dependency check failed — {result.stderr.strip()}", file=sys.stderr)
        print("On some systems 'python3' must be used instead of 'python'.", file=sys.stderr)
        sys.exit(1)
    missing = [m for m in result.stderr.strip().split("\n") if m]
    if missing:
        print(
            f"ERROR: Missing or broken dependencies: {', '.join(missing)}.\n"
            f"Install with: pip install {' '.join(missing)}\n"
            f"On some systems 'python3' must be used instead of 'python'.",
            file=sys.stderr,
        )
        sys.exit(1)


_exit_reason_map = {
    "STOPPED": "Stop Loss",
    "CLOSED": "Take Profit",
    "TIMEOUT": "Strategy Exit",
}


def _holding_time_from_position(position: Any) -> "timedelta":
    """Holding duration for a close notification, from the entry timestamp.

    The reconciled position dict never carries ``holding_hours``
    (``reconcile_position`` does not set it) and the paper engine's
    ``VirtualPosition`` records do not persist it — so the close
    notification used to report "0s" even for a position held for months,
    while ``/positions`` (which reads ``opened_at``) showed the real
    duration. Derive the duration from ``entry_time`` / ``opened_at`` the
    same way the positions command does, falling back to ``holding_hours``
    only when no timestamp exists.
    """
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415

    if isinstance(position, dict):
        entry_raw = position.get("entry_time") or position.get("opened_at", "")
        holding_hours = position.get("holding_hours", 0) or 0
    else:
        entry_raw = (
            getattr(position, "entry_time", "")
            or getattr(position, "opened_at", "")
            or ""
        )
        holding_hours = getattr(position, "holding_hours", 0) or 0

    if entry_raw:
        try:
            dt = datetime.fromisoformat(str(entry_raw).split("+")[0].split("Z")[0])
            holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
            if holding.total_seconds() > 0:
                return holding
        except (ValueError, TypeError):
            pass

    if holding_hours:
        return timedelta(hours=float(holding_hours))
    return timedelta()


def _notify_closure(
    logger: Any,
    notifier: Any,
    symbol: str,
    new_pos: Any,
    exit_price: float,
    exit_reason_map: dict[str, str],
    config: Any = None,
) -> None:
    """Send Telegram notification when a position closes."""
    quote = (getattr(config, "quote_currency", None) or os.getenv("QUOTE_CURRENCY", "USDT")).upper() if config else os.getenv("QUOTE_CURRENCY", "USDT").upper()
    exit_reason = exit_reason_map.get(new_pos.status, "Strategy Exit")
    # "CLOSED" can be from TP (tp3_hit) or trend_exit — use tp3_hit + PnL
    if exit_reason == "Take Profit":
        if not new_pos.tp3_hit:
            exit_reason = "Strategy Exit"
        elif (new_pos.total_pnl or 0) < 0:
            exit_reason = "Strategy Exit"
    logger.info(
        f"Position {symbol}: {new_pos.status} "
        f"(PnL: {new_pos.total_pnl:+.2f} {quote}, {exit_reason})"
    )

    # Update paper balance & orders to reflect the closure
    pnl, new_balance = _update_paper_on_closure(
        logger, symbol, new_pos, exit_price, exit_reason,
    )

    # Send Telegram notification via centralized notifier
    try:
        notifier.notify_position_closed(
            symbol=symbol,
            entry_price=new_pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=new_pos.floating_pnl_pct,
            balance=new_balance,
            exit_reason=exit_reason,
            holding_time=_holding_time_from_position(new_pos),
        )
    except Exception as exc:
        logger.warning(f"Failed to send close notification for {symbol}: {exc}")


@paper_state_writes
def _update_paper_on_closure(
    logger: Any,
    symbol: str,
    new_pos: Any,
    exit_price: float,
    exit_reason: str,
) -> tuple[float, float]:
    """Update paper balance and order history when a position closes.

    Idempotency guard (BUG-2): the pipeline's reconciliation AND the
    monitor can close the same position in the same cycle (e.g. both
    read positions.json as OPEN, both trigger the SL). If the pipeline's
    provider already closed and credited the position (paper_state.json
    shows it CLOSED), this function MUST NOT credit the wallet a second
    time — it only keeps the positions.json/order-file bookkeeping in
    sync and returns the pnl for notification.

    Ghost positions (simulated by the Position stage but never executed
    — absent from paper_state.json) must also never credit the wallet,
    because their buy cost was never deducted.

    Returns:
        Tuple of (pnl, new_balance) for the caller to use in notifications.
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    now_ts = datetime.now(timezone.utc).isoformat()

    # ── Idempotency / ghost guard ────────────────────────────────────
    # paper_state.json is the authoritative record of executed trades.
    # A position that is CLOSED there was already credited by the
    # provider's execute_sell; a position absent from there was never
    # bought at all. In both cases skip every wallet mutation.
    # When the ledger file does not exist at all (legacy/fresh setups)
    # fall back to the old monitor-only accounting behavior.
    try:
        with open("data/paper_state.json") as f:
            _state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _state = None
    if _state is not None:
        _vp = (_state.get("positions") or {}).get(symbol)
        already_handled = _vp is None or _vp.get("status") == "CLOSED"
    else:
        _vp = None
        already_handled = False
    if already_handled:
        pnl = (
            new_pos.get("total_pnl", 0.0)
            if isinstance(new_pos, dict)
            else getattr(new_pos, "total_pnl", 0.0)
        )
        # The paper-state ledger is the engine's finalized accounting. When
        # the engine already closed + credited this position, its persisted
        # total_pnl is authoritative — prefer it over the reconciled value
        # so the Telegram notification shows the SAME pnl the engine booked.
        if _vp is not None and _vp.get("total_pnl") is not None:
            try:
                pnl = float(_vp["total_pnl"])
            except (TypeError, ValueError):
                pass
        logger.debug(
            f"Closure of {symbol} already handled by the pipeline "
            f"(paper_state status={None if _vp is None else _vp.get('status')}) "
            "— skipping wallet credit to avoid double-counting"
        )

    remaining_qty = new_pos.get("remaining_qty", 0) if isinstance(new_pos, dict) else getattr(new_pos, "remaining_qty", 0)
    quantity = new_pos.get("quantity", 0) if isinstance(new_pos, dict) else getattr(new_pos, "quantity", 0)
    entry_price_val = new_pos.get("entry_price", 0) if isinstance(new_pos, dict) else getattr(new_pos, "entry_price", 0)
    floating_pnl_pct_val = new_pos.get("floating_pnl_pct", 0) if isinstance(new_pos, dict) else getattr(new_pos, "floating_pnl_pct", 0)
    entry_time_val = new_pos.get("entry_time") if isinstance(new_pos, dict) else getattr(new_pos, "entry_time", None)

    qty = remaining_qty if remaining_qty > 0 else quantity

    if not already_handled:
        # Use ExecutionModel for accurate fee/slippage calculation
        from scripts.paper_trading_engine import ExecutionModel  # noqa: PLC0415
        sell_result = ExecutionModel.sell(exit_price, qty)
        fill_price = sell_result["fill_price"]
        fee = sell_result["fee"]
        total_proceeds = sell_result["total_proceeds"]

        # Use same ExecutionModel for cost basis so both sides include slippage
        buy_result = ExecutionModel.buy(entry_price_val, qty)
        cost_basis = buy_result["total_cost"]
        pnl = total_proceeds - cost_basis
    else:
        fill_price = exit_price
        fee = 0.0
        total_proceeds = 0.0
        cost_basis = 0.0
        pnl = pnl

    # Update paper_balance.json
    try:
        with open("data/paper_balance.json") as f:
            pb = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pb = {
            "initial_balance": float(os.getenv("ACCOUNT_BALANCE", "10000")),
            "final_balance": float(os.getenv("ACCOUNT_BALANCE", "10000")),
            "final_equity": float(os.getenv("ACCOUNT_BALANCE", "10000")),
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        }

    pb["final_balance"] = round(pb.get("final_balance", float(os.getenv("ACCOUNT_BALANCE", "10000"))) + total_proceeds, 2)
    if not already_handled:
        pb["total_trades"] = pb.get("total_trades", 0) + 1
        if pnl > 0:
            pb["winning_trades"] = pb.get("winning_trades", 0) + 1
        else:
            pb["losing_trades"] = pb.get("losing_trades", 0) + 1
        total = pb.get("total_trades", 0)
        pb["win_rate"] = round(pb.get("winning_trades", 0) / total * 100, 2) if total else 0.0
        pb["realized_pnl"] = round(pb.get("realized_pnl", 0.0) + pnl, 2)

    # Use canonical MetricsManager.compute_snapshot() for ALL derived
    # accounting metrics (equity, unrealized_pnl, net_pnl, return_pct).
    from scripts.metrics_manager import MetricsManager

    other_open: list[dict[str, Any]] = []
    try:
        with open("data/positions.json") as f:
            pos_data = json.load(f)
        other_open = [
            p for p in pos_data.get("positions", [])
            if p.get("symbol") != symbol and is_open(p.get("status"))
        ]
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    initial = pb.get("initial_balance", float(os.getenv("ACCOUNT_BALANCE", "10000")))
    snapshot = MetricsManager.compute_snapshot(
        cash=pb["final_balance"],
        realized_pnl=pb.get("realized_pnl", 0.0),
        initial_balance=initial,
        open_positions=other_open,
        total_trades=pb.get("total_trades", 0),
        winning_trades=pb.get("winning_trades", 0),
        losing_trades=pb.get("losing_trades", 0),
        win_rate=pb.get("win_rate", 0.0),
        profit_factor=pb.get("profit_factor", 0.0),
        gross_profit=pb.get("gross_profit", 0.0),
        gross_loss=pb.get("gross_loss", 0.0),
    )
    pb["unrealized_pnl"] = round(snapshot.unrealized_pnl, 2)
    pb["final_equity"] = round(snapshot.equity, 2)
    pb["net_pnl"] = round(snapshot.net_pnl, 2)
    pb["total_return_pct"] = round(snapshot.total_return_pct, 2)

    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj("data/paper_balance.json", pb, indent=2)
    except OSError as exc:
        logger.warning(f"Failed to update paper_balance.json: {exc}")

    # Also update positions.json so /positions sees closure immediately
    try:
        with open("data/positions.json") as f:
            pos_data = json.load(f)
        pos_updated = False
        for p in pos_data.get("positions", []):
            if p.get("symbol") == symbol and is_open(p.get("status")):
                p["status"] = "CLOSED"
                p["remaining_qty"] = 0.0
                p["unrealized_pnl"] = 0.0
                p["realized_pnl"] = round(pnl, 2)
                p["total_pnl"] = round(pnl, 2)
                p["closed_at"] = now_ts
                pos_updated = True
                break
        if pos_updated:
            pos_data["total_positions"] = len(pos_data.get("positions", []))
            pos_data["active_count"] = sum(
                1 for p in pos_data.get("positions", [])
                if is_open(p.get("status"))
            )
            pos_data["closed_count"] = sum(
                1 for p in pos_data.get("positions", [])
                if not is_open(p.get("status"))
            )
            from scripts.paper_state_lock import atomic_write_json as _awj
            _awj("data/positions.json", pos_data, indent=2, default=str)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to update positions.json: {exc}")

    # Append closed order to paper_orders.json (only for a closure this
    # monitor actually executed — a pipeline-handled closure already has
    # its SELL order recorded by the provider's execute_sell).
    if already_handled:
        return pnl, pb["final_balance"]

    order = {
        "id": f"monitor-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "filled_quantity": qty,
        "entry_price": entry_price_val,
        "fill_price": round(fill_price, 6),
        "slippage": round(fill_price - exit_price, 6),
        "entry_fee": round(fee, 6),
        "exit_price": round(fill_price, 6),
        "exit_fee": 0.0,
        "total_proceeds": round(total_proceeds, 2),
        "net_pnl": round(pnl, 2),
        "net_pnl_pct": round(floating_pnl_pct_val, 2),
        "status": "CLOSED",
        "created_at": entry_time_val,
        "filled_at": entry_time_val,
        "closed_at": now_ts,
        "exit_reason": exit_reason,
        }
    try:
        with open("data/paper_orders.json") as f:
            orders_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        orders_data = {"orders": []}
    orders_data.setdefault("orders", []).append(order)
    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj("data/paper_orders.json", orders_data, indent=2)
    except OSError as exc:
        logger.warning(f"Failed to update paper_orders.json: {exc}")

    # Sync paper_state.json so restarted engine doesn't re-close this position
    _sync_paper_state_on_closure(logger, symbol, pnl, total_proceeds)

    return pnl, pb["final_balance"]


@paper_state_writes
def _sync_paper_state_on_closure(
    logger: Any,
    symbol: str,
    pnl: float,
    total_proceeds: float = 0.0,
) -> None:
    """Mark position CLOSED in paper_state.json and update wallet balance.

    When the monitor closes a position, paper_balance.json and
    positions.json are updated but paper_state.json is not.  On restart
    the paper trading engine restores stale OPEN VirtualPositions from
    paper_state.json and re-closes them, producing duplicate Telegram
    notifications.  This function patches paper_state.json in-place.

    The wallet balance is also updated so the next pipeline cycle
    sees the correct cash balance and doesn't drain remaining cash to $0.
    """
    state_path = "data/paper_state.json"
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    positions = state.get("positions") or {}
    vp = positions.get(symbol)
    if vp is None or vp.get("status") == "CLOSED":
        return

    # Update wallet balance with proceeds from the closed position
    # so the paper engine's next cycle sees the correct cash balance.
    old_balance = state.get("balance", 0.0)
    state["balance"] = round(old_balance + total_proceeds, 2)

    vp["status"] = "CLOSED"
    vp["remaining_qty"] = 0.0
    vp["closure_notified"] = True
    vp["realized_pnl"] = round(pnl, 2)
    vp["total_pnl"] = round(pnl, 2)
    vp["unrealized_pnl"] = 0.0

    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(state_path, state, indent=2, default=str)
    except OSError as exc:
        logger.warning(f"Failed to sync paper_state.json for {symbol}: {exc}")


def _notify_buy_opened(
    logger: Any,
    notifier: Any,
    symbol: str,
    entry_price: float,
    quantity: float,
    position_size: float,
    stop_loss: float,
    tp1: float,
    tp2: float = 0.0,
    tp3: float = 0.0,
) -> bool:
    """Send Telegram notification when a buy is opened.

    Returns True only when the notification was actually delivered, so
    callers can decide whether to mark the symbol as notified. A failed
    send must NOT be recorded as notified, otherwise the BUY_OPENED for
    that position is permanently lost (never retried on the next restart).
    """
    try:
        cfg = getattr(logger, 'config', None) or getattr(logger, '_config', None)
        return bool(notifier.notify_buy_opened(
            symbol=symbol,
            timeframe=getattr(cfg, 'timeframe', '') if cfg else "",
            exchange=getattr(cfg, 'exchange', '') if cfg else "",
            entry_price=entry_price,
            quantity=quantity,
            position_size=position_size,
            stop_loss=stop_loss,
            take_profit=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            reasons=["Pipeline execution"],
        ))
    except Exception as exc:
        logger.warning(f"Failed to send buy notification for {symbol}: {exc}")
        return False


def _notify_existing_positions(
    logger: Any,
    notifier: Any,
) -> None:
    """Send buy notifications for open positions not yet notified.

    Only positions that actually exist in the paper ledger
    (``paper_state.json``) are notified — a position that only exists in
    ``positions.json`` (simulated by the Position stage but never
    executed) is a ghost and must NOT generate a BUY_OPENED notification
    (BUG-4: the smoke-test ghost THRESHOLD/IDR + BTC/USDT entries never
    existed in the ledger yet still produced BUY_OPENED messages).
    """
    NOTIFIED_FILE = "data/.notified_buys"
    try:
        notified: set[str] = set()
        if os.path.exists(NOTIFIED_FILE):
            with open(NOTIFIED_FILE) as f:
                notified = set(line.strip() for line in f if line.strip())

        # Authoritative ledger: only positions here were actually bought.
        try:
            with open("data/paper_state.json") as f:
                _ledger = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _ledger = {}
        ledger_positions = _ledger.get("positions") or {}

        with open("data/positions.json") as f:
            data = json.load(f)

        for pos in data.get("positions", []):
            sym = pos.get("symbol", "")
            if sym in notified:
                continue
            if not is_open(pos.get("status")):
                continue

            vp = ledger_positions.get(sym)
            if vp is None or vp.get("status") != "OPEN":
                # Ghost position — simulated by the Position stage but
                # never executed in the paper ledger. No real BUY ever
                # happened, so no BUY_OPENED notification may be sent.
                logger.debug(
                    f"Skipping BUY_OPENED for ghost position {sym} "
                    "(not present as OPEN in paper_state.json)"
                )
                continue

            sent = _notify_buy_opened(
                logger, notifier,
                symbol=sym,
                entry_price=pos.get("entry_price", 0),
                quantity=pos.get("quantity", 0),
                position_size=pos.get("position_size_usdt", 0),
                stop_loss=pos.get("stop_loss", 0),
                tp1=pos.get("tp1", 0),
                tp2=pos.get("tp2", 0),
                tp3=pos.get("tp3", 0),
            )
            if not sent:
                # Delivery failed — do NOT record the symbol as notified,
                # so the BUY_OPENED is retried on the next restart instead
                # of being permanently lost.
                logger.warning(
                    f"BUY_OPENED delivery failed for {sym} — will retry "
                    "on next restart"
                )
                continue
            notified.add(sym)
            # Record under PAPER_STATE_LOCK atomically so a concurrent
            # writer (the pipeline scheduler — BUG-4) can never clobber
            # this entry with a stale read-modify-write.
            try:
                from scripts.paper_state_lock import add_notified_buy
                add_notified_buy(sym)
            except Exception as exc:
                logger.debug(
                    f"Failed to record notified symbol {sym}: {exc}"
                )
    except Exception as exc:
        logger.debug(f"Notify existing positions failed: {exc}")


def _monitor_positions(
    logger: Any,
    notifier: Any,
    center: Any,
    container: Any = None,
) -> None:
    """Fetch current prices for open positions, update PnL, detect closures.
    Uses unified ExecutionPipeline for TP/SL logic (same for paper and live).
    """
    if not os.path.exists("data/positions.json"):
        return

    try:
        from scripts.execution_provider import (
            PaperExecutionProvider,
            LiveExecutionProvider,
            create_execution_provider,
        )
        from scripts.execution_pipeline import ExecutionPipeline
        from scripts.protection_manager import ProtectionManager
    except ImportError as exc:
        logger.debug(f"Monitor imports failed: {exc}")
        return

    # Load positions
    try:
        with open("data/positions.json") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    positions = data.get("positions", [])
    active = [p for p in positions if is_open(p.get("status"))]
    if not active:
        return

    # Load trade plans for reference prices
    plans_by_symbol: dict[str, dict] = {}
    try:
        with open("data/trade_plan.json") as f:
            plans_data = json.load(f)
        for p in plans_data.get("plans", []):
            plans_by_symbol[p["symbol"]] = p
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Fetch current prices from the configured exchange (non-binance
    # exchanges like indodax list symbols such as GOAT/IDR). Uses the
    # shared cached client + TTL ticker cache so the monitor, the
    # pipeline reconciliation and the health check collapse into a
    # single /api/pairs call and one rate-limit budget (429 fix).
    symbols = [p["symbol"] for p in active if "symbol" in p]
    try:
        from bot.data import fetch_tickers_cached
        _cfg = getattr(container, "_config", None)
        # Prefer the injected container config; fall back to .env file
        # (re-loaded via dotenv so an env-var override like EXCHANGE=indodax
        # from the shell does not silently override the configured exchange
        # for a position that belongs to a different exchange).
        _exchange_name = getattr(_cfg, "exchange", None) if _cfg is not None else None
        if not _exchange_name:
            from scripts.app_config import load_config as _load_cfg  # noqa: PLC0415
            try:
                _exchange_name = _load_cfg().exchange
            except Exception:
                _exchange_name = os.getenv("EXCHANGE", "binance")
        tickers = fetch_tickers_cached(_exchange_name, symbols)
    except Exception as exc:
        logger.debug(f"Monitor ticker fetch failed: {exc}")
        tickers = {}

    # If tickers came back empty (network hiccup, exchange down), build a
    # best-effort fallback from each position's last known current_price so
    # SL/TP reconciliation can still run and protect open positions.
    # Without this guard a transient network failure silently skips ALL
    # position monitoring until the next cycle.
    if not tickers:
        for p in active:
            sym = p.get("symbol", "")
            if sym and p.get("current_price", 0.0) > 0:
                tickers[sym] = {"last": p["current_price"]}
        if tickers:
            logger.debug(
                "Monitor: exchange unreachable — using last known prices "
                f"for {list(tickers.keys())}"
            )

    # Get the right provider
    is_live = (
        container is not None
        and hasattr(container, "order")
        and getattr(container.order, "mode", "PAPER") == "LIVE"
        # Same arm guard as the pipeline stage: the monitor must only use
        # the real-exchange provider when live trading is actually armed.
        # Otherwise TP/SL exits here would hit the exchange with real sell
        # orders for positions that were only simulated.
        and container.order.is_live_enabled()
    )
    if is_live:
        provider = LiveExecutionProvider(
            container.exchange,
            getattr(container, "_config", None),
        )
    else:
        provider = PaperExecutionProvider()

    # Account quote currency drives both the TP/SL currency guard in
    # reconcile_position (mismatched-currency positions are never closed
    # on stale prices) and any pipeline-side rejection message.
    _monitor_quote = None
    if container is not None:
        try:
            _monitor_quote = getattr(container, "_config", None).quote_currency
        except Exception:
            _monitor_quote = None
    if not _monitor_quote:
        try:
            from scripts.app_config import load_config as _load_cfg  # noqa: PLC0415
            _monitor_quote = _load_cfg().quote_currency
        except Exception:
            _monitor_quote = os.getenv("QUOTE_CURRENCY", "USDT")
    pipeline = ExecutionPipeline(
        provider,
        quote_currency=(_monitor_quote or "USDT").upper(),
    )

    if is_live:
        # LIVE exits go through the shared per-symbol gate so a TP/SL
        # market sell here can never duplicate the pipeline's or the
        # protection scheduler's (BUG-2). Protection orders are cancelled
        # BEFORE the market sell, inside the same critical section.
        _monitor_positions_live(
            logger, notifier, container,
            positions, tickers, plans_by_symbol, pipeline,
        )
        return

    changed = False

    for pos in positions:
        sym = pos.get("symbol", "")
        if not is_open(pos.get("status")):
            continue

        ticker = tickers.get(sym)
        if ticker is None:
            continue
        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            continue

        plan_data = plans_by_symbol.get(sym, {})
        old_status = pos.get("status")

        # Use unified reconciliation (shared TP/SL logic)
        reconciled = pipeline.reconcile_position(
            sym, current_price, pos, plan=plan_data,
        )

        if reconciled is None:
            continue

        # Update position state
        for key in (
            "current_price", "status", "floating_pnl", "floating_pnl_pct",
            "tp1_hit", "tp2_hit", "tp3_hit", "remaining_qty",
            "realized_pnl", "total_pnl", "unrealized_pnl",
        ):
            if key in reconciled:
                pos[key] = reconciled[key]

        changed = True

        # Detect closure → notify + cancel live protection
        new_status = reconciled.get("status")
        if (
            old_status != new_status
            and new_status in CLOSED_STATUSES
            and not pos.get("closure_notified", False)
        ):
            from datetime import datetime
            exit_reason = _exit_reason_map.get(new_status, "Strategy Exit")
            quote = os.getenv("QUOTE_CURRENCY", "USDT").upper()
            logger.info(
                f"Position {sym}: {old_status} → {new_status} "
                f"(PnL: {reconciled.get('total_pnl', 0):+.2f} {quote}, {exit_reason})"
            )

            pnl, new_balance = _update_paper_on_closure(
                logger, sym, reconciled, current_price, exit_reason,
            )

            try:
                notifier.notify_position_closed(
                    symbol=sym,
                    entry_price=reconciled.get("entry_price", 0),
                    exit_price=current_price,
                    pnl=pnl,
                    pnl_pct=reconciled.get("floating_pnl_pct", 0),
                    balance=new_balance,
                    exit_reason=exit_reason,
                    holding_time=_holding_time_from_position(reconciled),
                )
            except Exception as exc:
                logger.warning(f"Failed to send close notification for {sym}: {exc}")

            pos["closure_notified"] = True

            # Cancel live protection if position closed
            if is_live:
                try:
                    pm = ProtectionManager(
                        container.exchange,
                        getattr(container, "_config", None),
                    )
                    pm.cancel_protection(sym, reason="monitor_closure")
                except Exception as exc:
                    logger.debug(f"Cancel protection for {sym}: {exc}")

    if changed:
        merge_positions(data.get("positions", []))


def _live_quote_balance(container: Any) -> float:
    """Best-effort LIVE account balance in the quote currency.

    LIVE closures must NEVER read or write the paper accounting files
    (``paper_balance.json`` / ``paper_orders.json`` / ``paper_state.json``)
    — BUG-3. The "Balance now ..." line in a LIVE close notification
    therefore comes straight from the exchange, never from paper state.
    """
    try:
        config = getattr(container, "_config", None)
        quote = (
            getattr(config, "quote_currency", None)
            or os.getenv("QUOTE_CURRENCY", "USDT")
        ).upper()
        exchange = getattr(container, "exchange", None)
        if exchange is None:
            return 0.0
        raw = exchange.fetch_balance()
        bucket = raw.get(quote) if isinstance(raw, dict) else None
        if isinstance(bucket, dict):
            return float(bucket.get("total", 0) or 0)
        try:
            return float(bucket or 0)
        except (TypeError, ValueError):
            return 0.0
    except Exception:
        return 0.0


def _monitor_positions_live(
    logger: Any,
    notifier: Any,
    container: Any,
    positions: list[Any],
    tickers: Any,
    plans_by_symbol: dict[str, dict],
    pipeline: Any,
) -> None:
    """LIVE position monitor — serialized per symbol via exit_gate.

    Uses the SAME per-symbol lock and atomic positions.json updates as
    the pipeline reconciliation and the protection scheduler, so a TP/SL
    market sell here can never duplicate one of theirs (BUG-2). Resting
    protection orders are cancelled BEFORE the market sell.
    """
    from scripts.exit_gate import reconcile_exit  # noqa: PLC0415

    def _cancel_live_protection(symbol: str) -> None:
        try:
            from scripts.protection_manager import ProtectionManager  # noqa: PLC0415
            pm = ProtectionManager(
                container.exchange,
                getattr(container, "_config", None),
            )
            pm.cancel_protection(symbol, reason="exit_gate_reconcile")
        except Exception as exc:
            logger.debug(f"Cancel protection for {symbol}: {exc}")

    for pos in positions:
        sym = pos.get("symbol", "")
        if not is_open(pos.get("status")):
            continue

        ticker = tickers.get(sym)
        if ticker is None:
            continue
        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            continue

        plan_data = plans_by_symbol.get(sym, {})

        def _on_reconciled(
            prev: dict,
            reconciled: Any,
            _sym: str = sym,
            _price: float = current_price,
        ) -> None:
            _monitor_live_closure(
                logger, notifier, container, _sym, _price, prev, reconciled,
            )

        reconcile_exit(
            pipeline,
            sym,
            current_price,
            plan_data,
            cancel_protection=_cancel_live_protection,
            on_reconciled=_on_reconciled,
        )


def _monitor_live_closure(
    logger: Any,
    notifier: Any,
    container: Any,
    sym: str,
    current_price: float,
    prev: dict,
    reconciled: Any,
) -> None:
    """Closure handling for the LIVE monitor.

    Runs inside the per-symbol exit lock (via ``reconcile_exit``'s
    ``on_reconciled``), so follow-up bookkeeping (``closure_notified``)
    is persisted atomically with the reconcile.

    LIVE closures must NEVER touch the paper accounting files
    (``paper_balance.json`` / ``paper_orders.json`` / ``paper_state.json``)
    — BUG-3. PnL comes from the reconciled position (real fill data) and
    the "Balance now ..." line comes from the real exchange balance, so
    no paper file is read or written here.
    """
    from scripts.exit_gate import save_position  # noqa: PLC0415

    if reconciled is None:
        return

    old_status = prev.get("status")
    new_status = reconciled.get("status")
    if (
        old_status != new_status
        and new_status in CLOSED_STATUSES
        and not prev.get("closure_notified", False)
    ):
        exit_reason = _exit_reason_map.get(new_status, "Strategy Exit")
        quote = os.getenv("QUOTE_CURRENCY", "USDT").upper()
        pnl = float(reconciled.get("total_pnl", 0) or 0)
        new_balance = _live_quote_balance(container)
        logger.info(
            f"Position {sym}: {old_status} → {new_status} "
            f"(PnL: {pnl:+.2f} {quote}, {exit_reason})"
        )

        try:
            notifier.notify_position_closed(
                symbol=sym,
                entry_price=reconciled.get("entry_price", 0),
                exit_price=current_price,
                pnl=pnl,
                pnl_pct=reconciled.get("floating_pnl_pct", 0),
                balance=new_balance,
                exit_reason=exit_reason,
                holding_time=_holding_time_from_position(reconciled),
            )
        except Exception as exc:
            logger.warning(f"Failed to send close notification for {sym}: {exc}")

        merged = dict(reconciled)
        merged["closure_notified"] = True
        save_position(sym, merged)


def main() -> None:
    """Run ZetBot AI daemon with integrated Telegram Command Center."""
    _check_dependencies()
    t0 = time.time()

    # Load and validate configuration
    try:
        config = load_config()
        validate_config(config)
    except ConfigError as exc:
        print(f"ERROR: Configuration validation failed:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Failed to load configuration — {exc}", file=sys.stderr)
        sys.exit(1)

    # Initialize logger
    logger = PipelineLogger(config)
    git_commit = "?"
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            git_commit = r.stdout.strip()
    except Exception:
        pass
    logger.info(f"ZetBot AI — Daemon v0.5.0 (git: {git_commit})")
    logger.info(f"Python : {sys.version.split()[0]}")
    logger.info(f"Exchange : {config.exchange}")
    logger.info(f"Timeframe: {config.timeframe}")
    _pb = _read_json(os.path.join(config.data_dir, "paper_balance.json"))
    _live_balance = _pb.get("final_balance")
    _display_balance = (
        _live_balance if _live_balance is not None else float(config.account_balance)
    )
    logger.info(f"Balance  : {_display_balance:>8,.2f} {config.quote_currency}")
    logger.info(f"Mode     : {'PAPER' if config.paper_mode else 'LIVE'}")

    # ------------------------------------------------------------------
    #  PID lock — prevent duplicate instances
    # ------------------------------------------------------------------

    pid_file = PidFile(os.path.join(config.data_dir, "zetbot.pid"))
    if not pid_file.acquire():
        logger.error(
            f"Another ZetBot instance is already running "
            f"(PID file: {config.data_dir}/zetbot.pid) — exiting",
        )
        sys.exit(1)
    atexit.register(pid_file.release)
    logger.info("PID lock acquired")

    # ------------------------------------------------------------------
    #  Shutdown coordination
    # ------------------------------------------------------------------

    shutdown = threading.Event()

    # If a signal arrived during the import phase (before the main
    # signal handler below was installed), the early handler already
    # set _early_shutdown_event.  Propagate that into the main shutdown
    # event so the loop exits immediately.
    if _early_shutdown_event.is_set():
        shutdown.set()

    # Clean up stale shutdown signal file from a *previous* run only.
    # Files created after this process launched (e.g. by a test or
    # external tool during our import phase) must be preserved so the
    # keep-alive loop can observe them.
    stale_file = "data/.shutdown_requested"
    if os.path.exists(stale_file):
        try:
            if os.path.getmtime(stale_file) < _PROCESS_LAUNCH_TIME:
                os.remove(stale_file)
                logger.info("Cleaned up stale shutdown signal file from previous run")
        except OSError:
            pass

    _force_exit_timer: threading.Timer | None = None

    def _arm_force_exit_timer() -> None:
        """Guarantee the process exits within a bounded time even if
        graceful shutdown hangs (e.g. a worker thread fails to join).

        Shared by both the signal-based shutdown path and the shutdown
        signal-file path so neither can hang indefinitely. Daemon so a
        leftover timer can never by itself keep the process alive;
        cancelled once shutdown finishes cleanly.
        """
        nonlocal _force_exit_timer
        if _force_exit_timer is None or not _force_exit_timer.is_alive():
            _force_exit_timer = threading.Timer(15.0, os._exit, args=[1])
            _force_exit_timer.daemon = True
            _force_exit_timer.start()

    def _shutdown_handler(signum: int, _frame: Any) -> None:
        logger.info(f"Signal {signum} received — shutting down")
        _early_shutdown_event.set()
        shutdown.set()
        # Force exit after 15s if graceful shutdown doesn't complete.
        _arm_force_exit_timer()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    # ------------------------------------------------------------------
    #  Watchdog heartbeat — dedicated daemon thread.
    #
    #  Started as early as possible (before the slow ServiceContainer /
    #  paper-engine startup) so the heartbeat is fresh from the first
    #  second.  It runs independently of position monitoring: a monitor
    #  that is slow, raises, or hangs on an exchange call must NEVER stop
    #  heartbeat updates, or the watchdog kills a healthy bot (Telegram
    #  stays responsive in its own thread while the watchdog sees a stale
    #  heartbeat file and restarts the process).
    # ------------------------------------------------------------------
    _heartbeat_thread = _start_heartbeat_writer(shutdown, logger)
    logger.info(f"Watchdog heartbeat writer started (every {HEARTBEAT_INTERVAL_SECONDS:.0f}s)")

    # ------------------------------------------------------------------
    #  Service Container — single source of all dependencies
    # ------------------------------------------------------------------

    container = ServiceContainer(config, logger)
    container.bootstrap()
    logger.info("Service Container initialised")

    # LIVE mode: print the REAL exchange balance, never the stale paper
    # figure or the configured starting balance — the exact complaint the
    # operator had (banner said 300,000 while the account holds 496,672).
    # Reuses container.wallet (the SAME instance the bot trades with, with
    # its TTL cache) instead of a second exchange connection. TEST_MODE
    # skips the network call so subprocess tests stay deterministic.
    if config.paper_mode:
        _pb_bal = _read_json(os.path.join(config.data_dir, "paper_balance.json"))
        _display_balance = _pb_bal.get("final_balance")
        if _display_balance is None:
            _display_balance = float(config.account_balance)
    else:
        _display_balance = float(config.account_balance)
        if os.getenv("TEST_MODE") != "true":
            try:
                _display_balance = container.wallet.balance
            except Exception:
                pass
    logger.info(f"Balance  : {_display_balance:>8,.2f} {config.quote_currency}")

    # ------------------------------------------------------------------
    #  Centralized Notifier — single instance for all Telegram messages
    # ------------------------------------------------------------------

    from bot.notifier import Notifier  # noqa: PLC0415
    _notifier = Notifier.from_config(config)
    container.inject_notifier(_notifier)

    # Send bot_started notification — mirrors the bot_stopped notification
    # sent on shutdown further below. Previously only bot_stopped existed,
    # so a run would show "BOT STOPPED" on exit with no matching
    # "BOT STARTED" on startup.
    try:
        import json as _json
        try:
            with open("data/paper_balance.json") as _f:
                _pb = _json.load(_f)
            _bal = _pb.get("final_balance", 0.0)
            _eq = _pb.get("final_equity", 0.0)
        except (FileNotFoundError, _json.JSONDecodeError):
            _bal = float(config.account_balance)
            _eq = float(config.account_balance)

        def _send_bot_started() -> None:
            try:
                _notifier.notify_bot_started(
                    symbol=f"{config.quote_currency} pairs",
                    timeframe=config.timeframe,
                    exchange=config.exchange,
                    balance=_bal,
                    equity=_eq,
                )
            except Exception:
                pass

        _tg_startup_thread = threading.Thread(
            target=_send_bot_started,
            name="TGStartupNotify",
            daemon=True,
        )
        _tg_startup_thread.start()
    except Exception:
        pass

    # ------------------------------------------------------------------
    #  LIVE mode status — surface this LOUDLY so nobody assumes they're
    #  trading live just because PAPER_MODE=false. Engine mode now
    #  follows config.paper_mode (Phase 1), but actual order execution
    #  still requires an explicit enable_live() call (Phase 5 — not
    #  wired to any startup flag on purpose) — until then, LIVE mode
    #  silently falls back to the simulation executor.
    # ------------------------------------------------------------------
    if container.order.mode == "LIVE":
        if container.order.is_live_enabled():
            live_error = container.order.validate_live_ready()
            if live_error:
                logger.error(f"LIVE mode ARMED but misconfigured: {live_error}")
            else:
                logger.info(
                    "LIVE mode ARMED — real orders will be submitted to "
                    f"{config.exchange}."
                )
        else:
            logger.info(
                "Mode is LIVE but live execution is NOT armed "
                "(enable_live() was never called) — orders will run "
                "through the simulation executor, not the real exchange."
            )

    # ------------------------------------------------------------------
    #  LIVE-armed state from a PREVIOUS session — NEVER auto-reactivate.
    #  A restart (crash, deploy, VPS reboot, ...) is not the operator
    #  choosing to keep trading real money. If the last session left
    #  live_armed.json with armed=true, reset it here and require a
    #  fresh /golive + CONFIRM LIVE before any real order can be placed.
    #  (in-memory LiveExecutor.ENABLED already defaults to False on
    #  every process start regardless — this just makes the reset and
    #  the reason for it visible/auditable.)
    # ------------------------------------------------------------------
    try:
        prev_live_state = container.order.read_live_armed_state()
    except Exception:
        prev_live_state = {}

    if prev_live_state.get("armed"):
        logger.warning(
            "Found LIVE-armed state from a previous session "
            f"(armed at {prev_live_state.get('time', '?')}) — resetting. "
            "Live trading requires /golive + CONFIRM LIVE again after "
            "every restart."
        )
        try:
            container.order.disarm_live(reason="process_restart")
        except Exception as exc:
            logger.error(f"Failed to reset live_armed.json on restart: {exc}")

    # ------------------------------------------------------------------
    #  Unprotected LIVE positions — DETECT ONLY, never auto-create SL/TP.
    #  A real position with no active protection needs an operator
    #  decision (what stop/target?), not the bot guessing on restart.
    # ------------------------------------------------------------------
    if container.order.mode == "LIVE":
        try:
            unprotected = container.order.find_unprotected_live_positions()
        except Exception as exc:
            unprotected = []
            logger.error(f"Could not check for unprotected live positions: {exc}")

        if unprotected:
            symbols = ", ".join(p.get("symbol", "?") for p in unprotected)
            logger.warning(
                f"LIVE positions with NO active protection found: {symbols} — "
                "these have real holdings but no tracked stop-loss/take-profit. "
                "Review manually (see data/live_protections.json / /positions)."
            )

    # ------------------------------------------------------------------
    #  Health Monitor — background thread
    # ------------------------------------------------------------------

    health = HealthMonitor(logger, config, interval=60.0, shutdown_event=shutdown, wallet=container.wallet)
    health.start()
    container.inject_health(health)
    logger.info("Health Monitor started (every 60s)")

    # ------------------------------------------------------------------
    #  Telegram Command Center — start BEFORE scheduler for responsiveness
    # ------------------------------------------------------------------

    center: Any = None
    tg_thread: threading.Thread | None = None

    test_mode = os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes")
    has_creds = bool(config.telegram_token and config.telegram_chat_id)

    if test_mode or (config.telegram_enabled and has_creds):
        from scripts.telegram_commands import TelegramCommandCenter

        center = TelegramCommandCenter(
            config,
            test_mode=test_mode,
            health_monitor=health,
            shutdown_event=shutdown,
            pid_file=pid_file,
            services=container,
        )
        tg_thread = _start_worker(
            "TelegramCmd",
            center.run,
            logger,
        )
        if tg_thread:
            logger.info("Telegram Command Center started (background thread)")
        else:
            logger.error("Failed to start Telegram Command Center")
    else:
        if config.telegram_enabled and not has_creds:
            logger.warning(
                "Telegram enabled but TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing"
                " — command center disabled"
            )
        else:
            logger.info("Telegram disabled — command center not started")

    # ------------------------------------------------------------------
    #  WhatsApp Command Center (via Twilio) — optional second channel,
    #  runs alongside Telegram and shares the same command set/registry.
    # ------------------------------------------------------------------

    wa_center: Any = None
    wa_thread: threading.Thread | None = None

    wa_has_creds = bool(
        config.twilio_account_sid and config.twilio_auth_token
        and config.twilio_whatsapp_from and config.whatsapp_allowed_numbers
    )

    if test_mode or (config.whatsapp_enabled and wa_has_creds):
        from whatsapp.whatsapp_commands import WhatsAppCommandCenter

        wa_center = WhatsAppCommandCenter(
            config,
            test_mode=test_mode,
            health_monitor=health,
            shutdown_event=shutdown,
            pid_file=pid_file,
            services=container,
        )
        wa_thread = _start_worker(
            "WhatsAppCmd",
            wa_center.run,
            logger,
        )
        if wa_thread:
            logger.info("WhatsApp Command Center started (background thread)")
        else:
            logger.error("Failed to start WhatsApp Command Center")
    else:
        if config.whatsapp_enabled and not wa_has_creds:
            logger.warning(
                "WhatsApp enabled but TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN /"
                " TWILIO_WHATSAPP_FROM / WHATSAPP_ALLOWED_NUMBERS missing"
                " — command center disabled"
            )
        else:
            logger.info("WhatsApp disabled — command center not started")

    # ------------------------------------------------------------------
    #  Startup — load state, recover positions, check TP/SL, update stats
    # ------------------------------------------------------------------

    if config.paper_mode:
        try:
            from scripts.paper_trading_engine import main as paper_main

            def _paper_startup_worker() -> None:
                """Run the startup paper engine; failures are non-fatal."""
                try:
                    paper_main(
                        notifier=_notifier,
                        account_balance=config.account_balance,
                    )
                except Exception as exc:
                    logger.warning(
                        f"Paper engine startup failed (non-fatal): {exc}"
                    )

            # Run the startup engine in a DAEMON thread. The previous
            # ThreadPoolExecutor left a non-daemon worker alive past the 10s
            # timeout (shutdown(wait=False, cancel_futures=True) cannot stop a
            # running future), so it kept executing plans / reconciling TP/SL
            # concurrently with the monitor AND blocked sys.exit(0) at
            # shutdown. A daemon thread can never block interpreter exit.
            startup_thread = threading.Thread(
                target=_paper_startup_worker,
                name="PaperStartup",
                daemon=True,
            )
            startup_thread.start()
            startup_thread.join(timeout=10)
            if startup_thread.is_alive():
                logger.warning(
                    "Paper engine startup timed out — continuing "
                    "(background daemon thread)"
                )
            logger.info("Paper engine state restored — positions recovered, TP/SL checked")
        except Exception as exc:
            logger.warning(f"Paper engine startup failed (non-fatal): {exc}")

        # Strictly reconcile positions.json against paper_state.json.
        # Every positions.json writer merges by symbol and never removes,
        # so a record whose symbol is absent from paper_state.json (legacy
        # leftover, or the survivor of a partial state reset) would stay
        # visible to /positions forever. Drop those ghosts here, on every
        # startup, so Telegram only ever shows positions the paper engine
        # actually knows about.
        try:
            from scripts.paper_state_lock import sync_positions_from_state
            if sync_positions_from_state():
                logger.info(
                    "Reconciled positions.json with paper_state.json "
                    "(ghost positions removed / counters refreshed)"
                )
        except Exception as exc:
            logger.debug(f"Position state sync failed (non-fatal): {exc}")

        # Reconcile accounting files: detect stale equity, mismatched
        # initial_balance, invalid profit_factor, and repair in-place.
        try:
            from scripts.accounting_reconcile import reconcile
            findings = reconcile(logger_obj=logger, account_balance=config.account_balance)
            if findings.get("repairs_applied", 0) > 0:
                logger.info(
                    f"Accounting reconciliation applied "
                    f"{findings['repairs_applied']} repair(s)"
                )
        except Exception as exc:
            logger.warning(f"Accounting reconciliation failed (non-fatal): {exc}")

        # Prune old closed positions from positions.json to keep it lean.
        # Anything over 50 closed entries is archived to positions_archive.json.
        try:
            from scripts.paper_state_lock import prune_closed_positions
            pruned = prune_closed_positions(max_closed=50)
            if pruned > 0:
                logger.info(f"Pruned {pruned} old closed positions to positions_archive.json")
        except Exception as exc:
            logger.debug(f"Position pruning failed (non-fatal): {exc}")

        # Send BUY notifications for any open positions from prior sessions
        # that weren't notified yet (deduplicated via data/.notified_buys).
        try:
            _notify_existing_positions(logger, _notifier)
        except Exception as exc:
            logger.debug(f"Notify existing positions failed (non-fatal): {exc}")
    else:
        logger.info("Live mode — skipping paper engine startup")

    # ------------------------------------------------------------------
    #  Scheduler — automatic periodic pipeline execution
    # ------------------------------------------------------------------

    scheduler: Any = None
    if config.auto_pipeline:
        from scripts.pipeline_scheduler import PipelineScheduler

        scheduler = PipelineScheduler(
            pipeline_fn=container.run_pipeline,
            interval=config.pipeline_interval_seconds,
            logger=logger,
            shutdown_event=shutdown,
        )
        container.inject_scheduler(scheduler)
        scheduler.start()

    # ------------------------------------------------------------------
    #  Daily report scheduler — sends Telegram summary at 00:00 WIB
    # ------------------------------------------------------------------
    daily_report_scheduler: Any = None
    if _notifier is not None and getattr(_notifier, "enabled", False):
        try:
            from scripts.daily_report import DailyReportScheduler  # noqa: PLC0415
            daily_report_scheduler = DailyReportScheduler(
                notifier=_notifier,
                data_dir=config.data_dir,
                shutdown_event=shutdown,
            )
            daily_report_scheduler.start()
        except Exception as exc:
            logger.warning("DailyReportScheduler failed to start: %s", exc)

    # ------------------------------------------------------------------
    #  Backup scheduler — automatic hourly backups (P1 Reliability).
    #
    #  Previously the hourly BackupScheduler was never wired into the
    #  daemon, so backups only ran when an operator triggered the manual
    #  Telegram /backup (or `python main.py --backup`). Start it on every
    #  run so config/data/log snapshots are taken automatically, pruning
    #  archives older than BACKUP_RETENTION_DAYS. Honors the existing
    #  BACKUP_INTERVAL_SECONDS / BACKUP_RETENTION_DAYS env vars via the
    #  module defaults. A failure here must never block startup.
    # ------------------------------------------------------------------
    backup_scheduler: Any = None
    try:
        from scripts.backup_restore import (  # noqa: PLC0415
            BackupScheduler,
            BACKUP_INTERVAL_SECONDS,
            BACKUP_RETENTION_DAYS,
        )
        backup_scheduler = BackupScheduler(
            interval_seconds=BACKUP_INTERVAL_SECONDS,
            retention_days=BACKUP_RETENTION_DAYS,
            shutdown_event=shutdown,
        )
        backup_scheduler.start()
    except Exception as exc:
        logger.warning("BackupScheduler failed to start: %s", exc)

    # ------------------------------------------------------------------
    #  Protection reconciliation scheduler — LIVE mode only.
    #
    #  This is what actually makes the synthetic-OCO in
    #  scripts/protection_manager.py hold: without it, the sibling
    #  stop-loss/take-profit order only gets cancelled when someone
    #  runs /protectioncheck by hand. Started only when the engine mode
    #  is already LIVE (not just because PAPER_MODE=false was set —
    #  reconcile_all_protections() itself also no-ops in PAPER as a
    #  second safety net either way).
    # ------------------------------------------------------------------
    protection_scheduler: Any = None
    if container.order.mode == "LIVE":
        from scripts.protection_scheduler import ProtectionScheduler

        protection_scheduler = ProtectionScheduler(
            order_manager=container.order,
            interval=config.protection_reconcile_interval_seconds,
            logger=logger,
        )
        protection_scheduler.start()

    # ------------------------------------------------------------------
    #  Keep alive — monitor workers, health, shutdown
    # ------------------------------------------------------------------

    logger.info("ZetBot AI is running. Press Ctrl+C to stop.")

    _debug_shutdown = os.getenv("DEBUG_SHUTDOWN", "").lower() in ("1", "true", "yes")
    if _debug_shutdown:
        logger.info(
            f"[shutdown-debug] cwd={os.getcwd()!r} "
            f"watch_path={os.path.abspath('data/.shutdown_requested')!r} "
            f"main_thread={threading.current_thread().name!r}"
        )

    _monitor_interval = 0  # counter for periodic position monitoring (~60s)
    _debug_tick = 0
    try:
        while not shutdown.is_set():
            if _debug_shutdown:
                _debug_tick += 1
                logger.info(
                    f"[shutdown-debug] tick={_debug_tick} "
                    f"exists={os.path.exists('data/.shutdown_requested')}"
                )

            # -- Check for shutdown signal file (before blocking wait) ----
            if os.path.exists("data/.shutdown_requested"):
                logger.info("Shutdown signal file detected — shutting down")
                shutdown.set()
                # Bound total shutdown time the same way the signal
                # handlers do, in case a worker thread fails to join.
                _arm_force_exit_timer()
                # Do NOT remove .shutdown_requested here.  The file must
                # persist until the watchdog has observed it so that the
                # supervisor knows the stop was deliberate (BUG A fix).
                # Stale files from previous runs are cleaned up at next
                # startup (lines 1033-1040) and watchdog._do_restart
                # clears it before crash-restarts.
                break

            shutdown.wait(timeout=1.0)
            if shutdown.is_set():
                break

            # -- Position monitoring every ~60 seconds ------------------
            _monitor_interval += 1
            if _monitor_interval >= 60:
                _monitor_interval = 0
                # Monitoring is best-effort: an exception here must never
                # take down the keep-alive loop (a dead monitor used to
                # stop heartbeat writes and make the watchdog kill a
                # healthy bot).  The heartbeat is written by its own
                # dedicated thread (_start_heartbeat_writer), so it stays
                # fresh even while monitoring is failing or hung.
                try:
                    _monitor_positions(logger, _notifier, center, container=container)
                except Exception as exc:
                    logger.warning(f"Position monitoring failed: {exc}")

            # -- Watchdog: monitor Telegram thread -----------------------
            if tg_thread is not None and not tg_thread.is_alive():
                logger.warning(
                    "[WATCHDOG] TelegramCmd worker stopped — restarting"
                )
                from scripts.telegram_commands import TelegramCommandCenter

                center = TelegramCommandCenter(
                    config,
                    test_mode=test_mode,
                    health_monitor=health,
                    shutdown_event=shutdown,
                    pid_file=pid_file,
                    services=container,
                )
                new_thread = _start_worker(
                    "TelegramCmd",
                    center.run,
                    logger,
                )
                if new_thread:
                    tg_thread = new_thread
                    logger.info(
                        "[WATCHDOG] TelegramCmd restart successful"
                    )
                else:
                    logger.error(
                        "[WATCHDOG] TelegramCmd restart failed "
                        "(zombie detected)"
                    )
    except KeyboardInterrupt:
        shutdown.set()

    # ------------------------------------------------------------------
    #  Graceful shutdown
    # ------------------------------------------------------------------

    logger.info("Shutting down...")

    # Signal all workers to stop immediately
    shutdown.set()

    # Send bot_stopped notification BEFORE stopping workers
    try:
        import json as _json
        try:
            with open("data/paper_balance.json") as _f:
                _pb = _json.load(_f)
            _bal = _pb.get("final_balance", 0.0)
            _eq = _pb.get("final_equity", 0.0)
        except (FileNotFoundError, _json.JSONDecodeError):
            _bal = float(config.account_balance)
            _eq = float(config.account_balance)
        _notifier.notify_bot_stopped(cycles=0, balance=_bal, equity=_eq)
    except Exception:
        pass

    # Stop Telegram command center first (stop polling loop immediately)
    if center:
        logger.info("Stopping Telegram Command Center...")
        center.stop()
        if tg_thread and tg_thread.is_alive():
            tg_thread.join(timeout=3.0)
            if tg_thread.is_alive():
                logger.warning(
                    "Telegram thread did not stop within timeout"
                )
            else:
                logger.info("Telegram thread stopped cleanly")

    # Stop health monitor (sets shutdown_event to wake up health thread)
    if scheduler:
        logger.info("Stopping Pipeline Scheduler...")
        scheduler.stop()

    if protection_scheduler:
        logger.info("Stopping Protection Scheduler...")
        protection_scheduler.stop()

    if daily_report_scheduler:
        try:
            daily_report_scheduler.stop()
        except Exception:
            pass

    if backup_scheduler:
        try:
            logger.info("Stopping Backup Scheduler...")
            backup_scheduler.stop()
        except Exception:
            pass

    health.stop()

    # Remove PID file
    pid_file.release()
    logger.info("PID lock released")

    logger.info("ZetBot AI stopped")

    # Graceful shutdown finished in time — disarm the force-exit safety
    # net so it doesn't fire os._exit(1) a few seconds from now and
    # turn this clean shutdown into a reported crash/timeout.
    if _force_exit_timer is not None:
        _force_exit_timer.cancel()

    # Suppress stdout/stderr to prevent late print() calls from
    # crashing during interpreter finalization (Fatal Python error:
    # _enter_buffered_busy).  After this point log messages go to
    # devnull but still reach the log file.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        null = os.devnull
        sys.stdout = open(null, "w")
        sys.stderr = open(null, "w")
    except OSError:
        pass

    sys.exit(0)


# ---------------------------------------------------------------------------
#  CLI Entry Points
# ---------------------------------------------------------------------------


def _cli_setup() -> None:
    from scripts.setup_wizard import run_setup_wizard
    run_setup_wizard()


def _cli_config() -> None:
    from scripts.config_manager import display_config
    print(display_config())


def _cli_reset_config() -> None:
    from scripts.config_manager import reset_env, display_config
    from scripts.setup_wizard import run_setup_wizard
    reset_env(backup=True)
    print("Previous configuration backed up.\n")
    run_setup_wizard()


def _cli_wizard() -> None:
    from scripts.wizard_menu import run_wizard_menu
    run_wizard_menu()


def _cli_diagnostics() -> None:
    from scripts.diagnostics import run_diagnostics
    result = run_diagnostics()
    result.print_report()


def _cli_backup() -> None:
    from scripts.backup_restore import create_backup
    try:
        path = create_backup()
        print(f"Backup created: {path}")
    except Exception as exc:
        print(f"Backup failed: {exc}")
        sys.exit(1)


def _cli_restore() -> None:
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 main.py --restore <backup.zip>")
        sys.exit(1)
    backup_path = sys.argv[2]
    from scripts.backup_restore import restore_backup
    if not restore_backup(backup_path):
        sys.exit(1)


def _cli_export_config() -> None:
    from scripts.config_import_export import export_config
    include_secrets = "--include-secrets" in sys.argv
    password = None
    if "--password" in sys.argv:
        idx = sys.argv.index("--password")
        if idx + 1 < len(sys.argv):
            password = sys.argv[idx + 1]
    try:
        path = export_config(include_secrets=include_secrets, password=password)
        print(f"Configuration exported to {path}")
    except Exception as exc:
        print(f"Export failed: {exc}")
        sys.exit(1)


def _cli_import_config() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 main.py --import-config <file.json> [--password <pass>] [--force]")
        sys.exit(1)
    config_path = sys.argv[2]
    password = None
    if "--password" in sys.argv:
        idx = sys.argv.index("--password")
        if idx + 1 < len(sys.argv):
            password = sys.argv[idx + 1]
    force = "--force" in sys.argv
    from scripts.config_import_export import import_config
    if not import_config(config_path, password=password, force=force):
        sys.exit(1)


def _cli_test_exchange() -> None:
    from scripts.exchange_test import run_exchange_test
    result = run_exchange_test()
    print(result)


def _cli_test_telegram() -> None:
    from scripts.telegram_test import run_telegram_test
    result = run_telegram_test()
    print(f"\n=== Telegram Connection Test ===\n")
    print(f"  {result}")


def _cli_system_info() -> None:
    from scripts.system_info import get_system_info
    info = get_system_info()
    print(info)


CLI_DISPATCH: dict[str, callable] = {
    "--setup": _cli_setup,
    "--config": _cli_config,
    "--reset-config": _cli_reset_config,
    "--wizard": _cli_wizard,
    "--diagnostics": _cli_diagnostics,
    "--backup": _cli_backup,
    "--restore": _cli_restore,
    "--export-config": _cli_export_config,
    "--import-config": _cli_import_config,
    "--test-exchange": _cli_test_exchange,
    "--test-telegram": _cli_test_telegram,
    "--system": _cli_system_info,
}


def _run_cli() -> None:
    """Dispatch to the appropriate CLI handler."""
    for flag, handler in CLI_DISPATCH.items():
        if flag in sys.argv:
            handler()
            os._exit(0)


if __name__ == "__main__":
    _run_cli()
    main()
