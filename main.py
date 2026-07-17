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
        1 for p in pos_list if p.get("status") == "OPEN"
    ) if isinstance(pos_list, list) else 0
    closed_positions = sum(
        1 for p in pos_list if p.get("status") in ("CLOSED", "STOPPED", "TIMEOUT")
    ) if isinstance(pos_list, list) else 0
    lines.append(f"Open positions      : {open_positions}")
    lines.append(f"Closed positions    : {closed_positions}")

    # Paper trading
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
    lines.append(f"Realized PnL        : ${realized:>+10,.2f}")
    lines.append(f"Unrealized PnL      : ${unrealized:>+10,.2f}")
    lines.append(f"Net PnL             : ${net_pnl:>+10,.2f}")
    lines.append(f"USDT balance        : ${balance:>10,.2f}")
    lines.append(f"Equity              : ${equity:>10,.2f}")
    lines.append(f"Cash                : ${balance:>10,.2f}")

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


def _send_telegram(config: AppConfig, lines: list[str]) -> None:
    """Send summary via Telegram if configured."""
    if not config.telegram_enabled:
        return
    try:
        from bot.telegram import TelegramNotifier
        import bot.config as bot_cfg

        bot_cfg.CONFIG.update({
            "telegram_enabled": True,
            "telegram_token": config.telegram_token,
            "telegram_chat_id": config.telegram_chat_id,
            "telegram_timeout": config.telegram_timeout,
            "telegram_retry": config.telegram_retry,
        })

        notifier = TelegramNotifier()
        msg = "\n".join(line for line in lines[:25])
        notifier._send(f"\U0001f4ca *Pipeline Report*\n{msg}")
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


def _notify_closure(
    logger: Any,
    config: AppConfig,
    symbol: str,
    new_pos: Any,
    exit_price: float,
    exit_reason_map: dict[str, str],
) -> None:
    """Send Telegram notification when a position closes."""
    exit_reason = exit_reason_map.get(new_pos.status, "Strategy Exit")
    logger.info(
        f"Position {symbol}: {new_pos.status} "
        f"(PnL: ${new_pos.total_pnl:+.2f}, {exit_reason})"
    )

    # Update paper balance & orders to reflect the closure
    _update_paper_on_closure(logger, symbol, new_pos, exit_price, exit_reason)

    # Send Telegram notification
    if not config.telegram_enabled:
        logger.debug(f"Telegram disabled, skipping close notification for {symbol}")
        return

    # Read latest balance after update
    balance = 0.0
    try:
        with open("data/paper_balance.json") as f:
            pb = json.load(f)
        balance = pb.get("final_balance", 0.0)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    try:
        from bot.telegram import TelegramNotifier  # noqa: PLC0415
        import bot.config as bot_cfg  # noqa: PLC0415

        bot_cfg.CONFIG.update({
            "telegram_enabled": config.telegram_enabled,
            "telegram_token": config.telegram_token,
            "telegram_chat_id": config.telegram_chat_id,
            "telegram_timeout": config.telegram_timeout,
            "telegram_retry": 3,
        })
        notifier = TelegramNotifier()
        from datetime import timedelta  # noqa: PLC0415
        holding_secs = new_pos.holding_hours * 3600
        notifier.trade_closed(
            exit_price=exit_price,
            pnl_usd=new_pos.total_pnl,
            pnl_pct=new_pos.floating_pnl_pct,
            balance=balance,
            exit_reason=exit_reason,
            holding_time=timedelta(seconds=holding_secs),
        )
    except Exception as exc:
        logger.warning(f"Failed to send close notification for {symbol}: {exc}")


def _update_paper_on_closure(
    logger: Any,
    symbol: str,
    new_pos: Any,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Update paper balance and order history when a position closes."""
    from datetime import datetime, timezone  # noqa: PLC0415
    now_ts = datetime.now(timezone.utc).isoformat()

    qty = new_pos.remaining_qty if new_pos.remaining_qty > 0 else new_pos.quantity

    # Use ExecutionModel for accurate fee/slippage calculation
    from scripts.paper_trading_engine import ExecutionModel  # noqa: PLC0415
    sell_result = ExecutionModel.sell(exit_price, qty)
    fill_price = sell_result["fill_price"]
    fee = sell_result["fee"]
    total_proceeds = sell_result["total_proceeds"]

    # Use same ExecutionModel for cost basis so both sides include slippage
    buy_result = ExecutionModel.buy(new_pos.entry_price, qty)
    cost_basis = buy_result["total_cost"]
    pnl = total_proceeds - cost_basis

    # Update paper_balance.json
    try:
        with open("data/paper_balance.json") as f:
            pb = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pb = {
            "initial_balance": 10000.0,
            "final_balance": 10000.0,
            "final_equity": 10000.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        }

    pb["final_balance"] = round(pb.get("final_balance", 10000.0) + total_proceeds, 2)
    pb["final_equity"] = pb["final_balance"]
    pb["total_trades"] = pb.get("total_trades", 0) + 1
    if pnl > 0:
        pb["winning_trades"] = pb.get("winning_trades", 0) + 1
    else:
        pb["losing_trades"] = pb.get("losing_trades", 0) + 1
    total = pb.get("total_trades", 0)
    pb["win_rate"] = round(pb.get("winning_trades", 0) / total * 100, 2) if total else 0.0
    pb["realized_pnl"] = round(pb.get("realized_pnl", 0.0) + pnl, 2)
    pb["net_pnl"] = round(pb.get("net_pnl", 0.0) + pnl, 2)

    try:
        with open("data/paper_balance.json", "w") as f:
            json.dump(pb, f, indent=2)
    except OSError as exc:
        logger.warning(f"Failed to update paper_balance.json: {exc}")

    # Append closed order to paper_orders.json
    order = {
        "id": f"monitor-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "filled_quantity": qty,
        "entry_price": new_pos.entry_price,
        "fill_price": round(fill_price, 6),
        "slippage": round(fill_price - exit_price, 6),
        "entry_fee": round(fee, 6),
        "exit_price": round(fill_price, 6),
        "exit_fee": 0.0,
        "total_proceeds": round(total_proceeds, 2),
        "net_pnl": round(pnl, 2),
        "net_pnl_pct": round(new_pos.floating_pnl_pct, 2),
        "status": "CLOSED",
        "created_at": new_pos.entry_time,
        "filled_at": new_pos.entry_time,
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
        with open("data/paper_orders.json", "w") as f:
            json.dump(orders_data, f, indent=2)
    except OSError as exc:
        logger.warning(f"Failed to update paper_orders.json: {exc}")


def _notify_buy_opened(
    logger: Any,
    config: AppConfig,
    symbol: str,
    entry_price: float,
    quantity: float,
    position_size: float,
    stop_loss: float,
    tp1: float,
) -> None:
    """Send Telegram notification when a buy is opened."""
    if not config.telegram_enabled:
        return

    try:
        from bot.telegram import TelegramNotifier  # noqa: PLC0415
        import bot.config as bot_cfg  # noqa: PLC0415

        bot_cfg.CONFIG.update({
            "telegram_enabled": config.telegram_enabled,
            "telegram_token": config.telegram_token,
            "telegram_chat_id": config.telegram_chat_id,
            "telegram_timeout": config.telegram_timeout,
            "telegram_retry": 3,
        })
        notifier = TelegramNotifier()
        notifier.buy_opened(
            symbol=symbol,
            timeframe=config.timeframe,
            exchange=config.exchange,
            entry_price=entry_price,
            quantity=quantity,
            position_size=position_size,
            stop_loss=stop_loss,
            take_profit=tp1,
            reasons=["Pipeline execution"],
        )
    except Exception as exc:
        logger.warning(f"Failed to send buy notification for {symbol}: {exc}")


def _notify_existing_positions(
    logger: Any,
    config: AppConfig,
) -> None:
    """Send buy notifications for open positions not yet notified."""
    NOTIFIED_FILE = "data/.notified_buys"
    try:
        notified: set[str] = set()
        if os.path.exists(NOTIFIED_FILE):
            with open(NOTIFIED_FILE) as f:
                notified = set(line.strip() for line in f if line.strip())

        with open("data/positions.json") as f:
            data = json.load(f)

        new_notified = False
        for pos in data.get("positions", []):
            sym = pos.get("symbol", "")
            if sym in notified:
                continue
            if pos.get("status") not in ("OPEN", "PARTIAL", "TRAILING", "BREAKEVEN"):
                continue

            _notify_buy_opened(
                logger, config,
                symbol=sym,
                entry_price=pos.get("entry_price", 0),
                quantity=pos.get("quantity", 0),
                position_size=pos.get("position_size_usdt", 0),
                stop_loss=pos.get("stop_loss", 0),
                tp1=pos.get("tp1", 0),
            )
            notified.add(sym)
            new_notified = True

        if new_notified:
            with open(NOTIFIED_FILE, "w") as f:
                for sym in sorted(notified):
                    f.write(f"{sym}\n")
    except Exception as exc:
        logger.debug(f"Notify existing positions failed: {exc}")


def _monitor_positions(
    logger: Any,
    config: AppConfig,
    center: Any,
) -> None:
    """Fetch current prices for open positions, update PnL, detect closures."""
    # Only run if pipeline already ran (positions.json exists)
    if not os.path.exists("data/positions.json"):
        return

    try:
        import ccxt  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        from scripts.position_manager import (  # noqa: PLC0415
            PositionSimulator, TradePlan,
        )
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
    active = [
        p for p in positions
        if p.get("status") in ("OPEN", "PARTIAL", "TRAILING", "BREAKEVEN")
    ]
    if not active:
        return

    # Load scanner prices for ATR/trend
    scanner_prices: dict[str, dict] = {}
    try:
        with open("data/scanner_results.json") as f:
            scan = json.load(f)
        for p in scan.get("pairs", []):
            scanner_prices[p["symbol"]] = p
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Load trade plans
    plans_by_symbol: dict[str, dict] = {}
    try:
        with open("data/trade_plan.json") as f:
            plans_data = json.load(f)
        for p in plans_data.get("plans", []):
            plans_by_symbol[p["symbol"]] = p
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Fetch tickers for active symbols
    symbols = [p["symbol"] for p in active if "symbol" in p]
    try:
        exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
            "timeout": 15000,
        })
        tickers = exchange.fetch_tickers(symbols)
    except Exception as exc:
        logger.debug(f"Monitor ticker fetch failed: {exc}")
        return

    now = datetime.now(timezone.utc)
    changed = False

    for pos in positions:
        sym = pos.get("symbol", "")
        if pos.get("status") not in ("OPEN", "PARTIAL", "TRAILING", "BREAKEVEN"):
            continue

        ticker = tickers.get(sym)
        if ticker is None:
            continue
        current_price = ticker.get("last")
        if current_price is None or current_price <= 0:
            continue

        scan_data = scanner_prices.get(sym, {})
        atr_pct = scan_data.get("atr_pct", 0.0)
        trend = scan_data.get("trend_alignment", "MIXED")
        plan_data = plans_by_symbol.get(sym, {})

        try:
            plan = TradePlan(
                symbol=sym,
                entry_price=plan_data.get("entry_price", pos.get("entry_price", 0.0)),
                position_size_usdt=plan_data.get("position_size_usdt", 0.0),
                quantity=plan_data.get("quantity", 0.0),
                stop_loss=plan_data.get("stop_loss", pos.get("stop_loss", 0.0)),
                tp1=plan_data.get("tp1", pos.get("tp1", 0.0)),
                tp2=plan_data.get("tp2", pos.get("tp2", 0.0)),
                tp3=plan_data.get("tp3", pos.get("tp3", 0.0)),
                risk_amount=plan_data.get("risk_amount", 0.0),
                reward_amount=plan_data.get("reward_amount", 0.0),
                risk_reward=plan_data.get("risk_reward", 0.0),
                probability=plan_data.get("probability", 0.0),
                recommendation=plan_data.get("recommendation", ""),
                confidence=plan_data.get("confidence", 0.0),
                signal_time=pos.get("entry_time", ""),
                status="",
                rejection_reason="",
            )
        except Exception as exc:
            logger.debug(f"Monitor plan creation failed for {sym}: {exc}")
            continue

        old_status = pos.get("status")
        new_pos = PositionSimulator.simulate(plan, current_price, atr_pct, trend, now)

        # Update position data
        pos["current_price"] = round(current_price, 8)
        pos["status"] = new_pos.status
        pos["floating_pnl"] = new_pos.floating_pnl
        pos["floating_pnl_pct"] = new_pos.floating_pnl_pct
        pos["holding_hours"] = new_pos.holding_hours
        pos["holding_candles"] = new_pos.holding_candles
        pos["tp1_hit"] = new_pos.tp1_hit
        pos["tp2_hit"] = new_pos.tp2_hit
        pos["tp3_hit"] = new_pos.tp3_hit
        pos["breakeven_active"] = new_pos.breakeven_active
        pos["trailing_active"] = new_pos.trailing_active
        pos["current_stop"] = round(new_pos.current_stop, 8)
        pos["remaining_pct"] = new_pos.remaining_pct
        pos["remaining_qty"] = new_pos.remaining_qty
        pos["realized_pnl"] = new_pos.realized_pnl
        pos["total_pnl"] = new_pos.total_pnl
        pos["highest_price"] = new_pos.highest_price
        pos["lowest_price"] = new_pos.lowest_price

        changed = True

        # Detect status change → notify closure once
        if (
             old_status != new_pos.status
    and new_pos.status in ("CLOSED", "STOPPED", "TIMEOUT")
    and not pos.get("closure_notified", False)
    ):
            _notify_closure(
                logger,
                config,
                sym,
                new_pos,
                current_price,
            _exit_reason_map,
    )
            pos["closure_notified"] = True

        if changed:
            try:
                with open("data/positions.json", "w") as f:
                    json.dump({"positions": positions}, f, indent=2)
            except OSError as exc:
                logger.error(f"Failed to write positions.json: {exc}")


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
    logger.info(f"Balance  : ${config.account_balance:>8,.2f}")
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
    #  Service Container — single source of all dependencies
    # ------------------------------------------------------------------

    container = ServiceContainer(config, logger)
    container.bootstrap()
    logger.info("Service Container initialised")

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

    health = HealthMonitor(logger, config, interval=60.0, shutdown_event=shutdown)
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
    #  Startup — load state, recover positions, check TP/SL, update stats
    # ------------------------------------------------------------------

    if config.paper_mode:
        try:
            from scripts.paper_trading_engine import main as paper_main
            import concurrent.futures
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(paper_main)
            try:
                fut.result(timeout=10)
            except concurrent.futures.TimeoutError:
                logger.warning("Paper engine startup timed out — continuing")
            except Exception as exc:
                logger.warning(f"Paper engine startup failed (non-fatal): {exc}")
            pool.shutdown(wait=False, cancel_futures=True)
            del pool
            logger.info("Paper engine state restored — positions recovered, TP/SL checked")
        except Exception as exc:
            logger.warning(f"Paper engine startup failed (non-fatal): {exc}")
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
                try:
                    os.remove("data/.shutdown_requested")
                except OSError:
                    pass
                break

            shutdown.wait(timeout=1.0)
            if shutdown.is_set():
                break

            # -- Position monitoring every ~60 seconds ------------------
            _monitor_interval += 1
            if _monitor_interval >= 60:
                _monitor_interval = 0
                _monitor_positions(logger, config, center)

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
    from scripts.exchange_test import test_exchange
    result = test_exchange()
    print(result)


def _cli_test_telegram() -> None:
    from scripts.telegram_test import test_telegram
    result = test_telegram()
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
