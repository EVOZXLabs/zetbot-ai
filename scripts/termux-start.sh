#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — Termux launcher / supervisor (operational layer, Android)
#
#  Starts the trading bot and its watchdog in two dedicated tmux sessions
#  (zetbot-bot, zetbot-watchdog), keeps a Termux wake-lock, cleans stale
#  watchdog flags, and can verify the whole crash->restart->notify pipeline
#  against the REAL Telegram API.
#
#  This script only *runs* the bot/watchdog. It does NOT touch any trading
#  or watchdog logic.
#
#  Usage:
#    termux-start.sh                  start bot + watchdog (idempotent)
#    termux-start.sh --status         show tmux sessions / pids / flags
#    termux-start.sh --stop           stop watchdog then bot (graceful)
#    termux-start.sh --verify         crash-test: kill bot, wait for watchdog
#                                     restart, confirm real Telegram delivery
#    termux-start.sh --help           this help
#
#  Requirements (checked at runtime):
#    * tmux            ->  pkg install tmux
#    * Termux:API      ->  pkg install termux-api  (+ install the Termux:API
#                          app from F-Droid, needed for termux-wake-lock)
#    * a configured .env with TELEGRAM_* for notifications (--verify needs
#      TELEGRAM_ENABLED=true + TELEGRAM_TOKEN + TELEGRAM_CHAT_ID)
#
#  Termux:Boot hook: copy scripts/termux-boot/zetbot-start.sh to
#  ~/.termux/boot/ so this launcher runs automatically after reboot.
# =============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BOT_SESSION="zetbot-bot"
WD_SESSION="zetbot-watchdog"

# Defaults (override via env)
STALE_FLAG_MINUTES="${ZETBOT_STALE_FLAG_MINUTES:-5}"
VERIFY_TIMEOUT="${ZETBOT_VERIFY_TIMEOUT:-120}"
BOT_START_WAIT="${ZETBOT_BOT_START_WAIT:-60}"
WD_START_WAIT="${ZETBOT_WD_START_WAIT:-30}"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-20}"

PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python || true)"

# Flags the watchdog/bot use as stop/pause/halt signals.
FLAG_SHUTDOWN="data/.shutdown_requested"
FLAG_HALT="data/.watchdog_halt"
FLAG_PAUSE="data/.watchdog_paused"

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
err() { printf '%s ERROR %s\n' "$(date '+%F %T')" "$*" >&2; }

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "'$1' is required but not installed."
        case "$1" in
            tmux) err "  Install it:  pkg install tmux" ;;
            termux-wake-lock)
                err "  Install Termux:API:  pkg install termux-api"
                err "  Then install the Termux:API app from F-Droid:"
                err "    https://f-droid.org/packages/com.termux.api/"
                err "  (also available on the Play Store), open it once,"
                err "  and re-run this script." ;;
            *) err "  Install it with:  pkg install $1" ;;
        esac
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
#  Termux wake-lock (keep the CPU alive; background processes survive Doze)
# ---------------------------------------------------------------------------

termux_wake_lock() {
    if ! need_cmd termux-wake-lock; then return 1; fi
    if command -v termux-api >/dev/null 2>&1; then
        log "termux-api package present"
    else
        log "note: the Termux:API *app* must be installed (F-Droid/Play Store)"
        log "      and opened once, otherwise termux-wake-lock is a no-op."
    fi
    if termux-wake-lock 2>/dev/null; then
        log "termux-wake-lock acquired (CPU stays awake while Termux runs)"
        return 0
    fi
    err "termux-wake-lock failed — the Termux:API app may be missing."
    return 1
}

# ---------------------------------------------------------------------------
#  Stale flag cleanup
#
#  Only removes a flag that is OLDER than STALE_FLAG_MINUTES. A halt / pause /
#  shutdown flag an operator created minutes ago is respected and kept.
# ---------------------------------------------------------------------------

clean_stale_flag() {
    local f="$1"
    [ -f "$f" ] || return 0
    if find "$f" -mmin +"$STALE_FLAG_MINUTES" -print -quit 2>/dev/null | grep -q .; then
        rm -f "$f" && log "cleaned stale flag (older than ${STALE_FLAG_MINUTES} min): $f"
    else
        log "kept flag (created < ${STALE_FLAG_MINUTES} min ago — respected): $f"
    fi
}

clean_stale_flags() {
    local f
    for f in "$FLAG_SHUTDOWN" "$FLAG_HALT" "$FLAG_PAUSE"; do
        clean_stale_flag "$ROOT/$f"
    done
}

# ---------------------------------------------------------------------------
#  Process / session detection (idempotency)
# ---------------------------------------------------------------------------

bot_running() {
    pgrep -f "main\.py" >/dev/null 2>&1
}

# NOTE: match "scripts/watchdog.py" (not just "watchdog.py") so a process
# running pytest on tests/test_watchdog.py is NOT mistaken for the watchdog.
watchdog_running() {
    pgrep -f "scripts/watchdog\.py" >/dev/null 2>&1
}

read_pid_file() {
    local f="$1" pid=""
    [ -f "$f" ] && pid="$(cat "$f" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) echo "" ;;
        *) echo "$pid" ;;
    esac
}

pid_alive() {
    local pid="$1"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null
}

wait_for_pid_file() {
    local f="$1" timeout="$2" n=0
    while [ "$n" -lt "$timeout" ]; do
        local pid; pid="$(read_pid_file "$f")"
        if [ -n "$pid" ] && pid_alive "$pid"; then
            echo "$pid"
            return 0
        fi
        sleep 1
        n=$((n + 1))
    done
    return 1
}

# ---------------------------------------------------------------------------
#  Start bot / watchdog in dedicated tmux sessions (idempotent)
# ---------------------------------------------------------------------------

start_bot() {
    if bot_running; then
        log "bot already running — skip (no duplicate session created)"
        return 0
    fi
    if tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
        log "recreating dead tmux session '$BOT_SESSION'"
        tmux kill-session -t "$BOT_SESSION" 2>/dev/null || true
    fi
    log "starting bot in tmux session '$BOT_SESSION'"
    tmux new-session -d -s "$BOT_SESSION" -c "$ROOT" \
        "$PY main.py 2>&1 | tee -a $ROOT/logs/bot-console.log"
    local pid
    if pid="$(wait_for_pid_file "$ROOT/data/zetbot.pid" "$BOT_START_WAIT")"; then
        log "bot up (pid $pid)"
        return 0
    fi
    err "bot did not write its PID file within ${BOT_START_WAIT}s — check:"
    err "    tmux attach -t $BOT_SESSION"
    return 1
}

start_watchdog() {
    if watchdog_running; then
        log "watchdog already running — skip (no duplicate session created)"
        return 0
    fi
    if tmux has-session -t "$WD_SESSION" 2>/dev/null; then
        log "recreating dead tmux session '$WD_SESSION'"
        tmux kill-session -t "$WD_SESSION" 2>/dev/null || true
    fi
    log "starting watchdog in tmux session '$WD_SESSION' (interval=${WATCHDOG_INTERVAL}s)"
    tmux new-session -d -s "$WD_SESSION" -c "$ROOT" \
        "$PY scripts/watchdog.py 2>&1 | tee -a $ROOT/logs/watchdog-console.log"
    local pid
    if pid="$(wait_for_pid_file "$ROOT/data/zetbot-watchdog.pid" "$WD_START_WAIT")"; then
        log "watchdog up (pid $pid)"
        return 0
    fi
    err "watchdog did not write its PID file within ${WD_START_WAIT}s — check:"
    err "    tmux attach -t $WD_SESSION"
    return 1
}

# ---------------------------------------------------------------------------
#  .env loader (exports KEY=VALUE for child processes; strips quotes)
# ---------------------------------------------------------------------------

load_env() {
    [ -f "$ROOT/.env" ] || return 1
    local key val
    while IFS='=' read -r key val; do
        case "$key" in
            ''|\#*) continue ;;
        esac
        key="${key%"${key##*[![:space:]]}"}"   # trim trailing spaces on key
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        export "$key=$val"
    done < "$ROOT/.env"
    return 0
}

# ---------------------------------------------------------------------------
#  Real Telegram send — exit 0 = Telegram API accepted the message
#  (the notifier returns the bool from requests.post() success + retries)
# ---------------------------------------------------------------------------

send_notify() {
    local msg="$1"
    ZETBOT_ROOT="$ROOT" "$PY" - "$msg" <<'PY'
import os
import sys

sys.path.insert(0, os.environ.get("ZETBOT_ROOT", os.getcwd()))

from bot.notifier import Notifier

n = Notifier.from_env()
if not n.enabled:
    print("NOTIFIER_DISABLED: set TELEGRAM_ENABLED=true + TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in .env")
    sys.exit(2)

ok = n.notify_system(sys.argv[1])
print("NOTIFIER_OK=%s" % ok)
sys.exit(0 if ok else 1)
PY
}

# ---------------------------------------------------------------------------
#  Status / stop
# ---------------------------------------------------------------------------

cmd_status() {
    echo "project root : $ROOT"
    echo "python       : $PY"

    for s in "$BOT_SESSION" "$WD_SESSION"; do
        if tmux has-session -t "$s" 2>/dev/null; then
            echo "tmux         : $s -> running"
        else
            echo "tmux         : $s -> absent"
        fi
    done

    local bot_pid wd_pid
    bot_pid="$(read_pid_file "$ROOT/data/zetbot.pid")"
    wd_pid="$(read_pid_file "$ROOT/data/zetbot-watchdog.pid")"
    if pid_alive "$bot_pid"; then echo "bot          : pid=$bot_pid alive=yes"; else echo "bot          : pid=${bot_pid:-none} alive=no"; fi
    if pid_alive "$wd_pid"; then echo "watchdog     : pid=$wd_pid alive=yes"; else echo "watchdog     : pid=${wd_pid:-none} alive=no"; fi

    local f
    for f in "$FLAG_SHUTDOWN" "$FLAG_HALT" "$FLAG_PAUSE"; do
        if [ -f "$ROOT/$f" ]; then
            echo "flag         : $f -> present (mtime $(date -r "$ROOT/$f" '+%F %T' 2>/dev/null || echo '?'))"
        else
            echo "flag         : $f -> absent"
        fi
    done

    if [ -f "$ROOT/logs/watchdog.log" ]; then
        echo "--- watchdog.log (last 5) ---"
        tail -n 5 "$ROOT/logs/watchdog.log" 2>/dev/null
    fi
}

cmd_stop() {
    log "stopping watchdog, then bot (graceful)"
    if tmux has-session -t "$WD_SESSION" 2>/dev/null; then
        tmux kill-session -t "$WD_SESSION" 2>/dev/null || true
    fi
    pkill -f "scripts/watchdog\.py" 2>/dev/null || true

    if tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
        tmux kill-session -t "$BOT_SESSION" 2>/dev/null || true
    fi
    local pid
    pid="$(read_pid_file "$ROOT/data/zetbot.pid")"
    if [ -n "$pid" ] && pid_alive "$pid"; then
        log "SIGTERM to bot pid $pid"
        kill "$pid" 2>/dev/null || true
        local n=0
        while pid_alive "$pid" && [ "$n" -lt 15 ]; do sleep 1; n=$((n + 1)); done
        if pid_alive "$pid"; then
            log "bot still alive after SIGTERM — SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    pkill -f "main\.py" 2>/dev/null || true
    log "stop complete"
}

# ---------------------------------------------------------------------------
#  --verify: real crash-test
#   1. confirm the Telegram channel works (notifier returns True == API 200)
#   2. SIGKILL the bot
#   3. wait for the watchdog to restart it (and NOT write the halt flag)
#   4. confirm Telegram delivery again after the restart
# ---------------------------------------------------------------------------

cmd_verify() {
    log "=== VERIFY: crash + restart + Telegram ==="

    if ! load_env; then
        err "no $ROOT/.env found — create it first (cp .env.example .env) and set TELEGRAM_*"
        return 3
    fi

    if ! bot_running; then
        log "bot not running — starting it first"
        start_bot || return 1
    fi
    if ! watchdog_running; then
        log "watchdog not running — starting it first"
        start_watchdog || return 1
    fi

    log "pre-check: sending a REAL Telegram test message via the notifier..."
    if ! send_notify "VERIFY (1/2) — notification channel OK, starting crash-test"; then
        err "pre-check notification FAILED — fix Telegram config (.env) and retry"
        return 2
    fi

    local old_pid
    old_pid="$(read_pid_file "$ROOT/data/zetbot.pid")"
    if ! pid_alive "$old_pid"; then
        old_pid="$(pgrep -f "main\.py" | head -1 || true)"
    fi
    if [ -z "$old_pid" ]; then
        err "could not find a running bot process to kill"
        return 1
    fi

    log "crash simulation: SIGKILL bot pid $old_pid"
    kill -9 "$old_pid" 2>/dev/null || true
    sleep 2

    log "waiting up to ${VERIFY_TIMEOUT}s for the watchdog to restart the bot..."
    local deadline=$(( $(date +%s) + VERIFY_TIMEOUT ))
    local new_pid="" halted=""
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ -f "$ROOT/$FLAG_HALT" ]; then halted=yes; break; fi
        new_pid="$(read_pid_file "$ROOT/data/zetbot.pid")"
        if [ -n "$new_pid" ] && [ "$new_pid" != "$old_pid" ] && pid_alive "$new_pid" \
            && grep -q "main.py" "/proc/$new_pid/cmdline" 2>/dev/null; then
            break
        fi
        sleep 2
    done

    if [ -n "$halted" ]; then
        err "FAIL: watchdog HALTED auto-restart (crash loop rate-limit). Fix the bot, remove $FLAG_HALT."
        tail -n 15 "$ROOT/logs/watchdog.log" 2>/dev/null >&2
        return 1
    fi
    if [ -z "$new_pid" ] || ! pid_alive "$new_pid"; then
        err "FAIL: watchdog did NOT restart the bot within ${VERIFY_TIMEOUT}s."
        tail -n 15 "$ROOT/logs/watchdog.log" 2>/dev/null >&2
        return 1
    fi

    log "OK: watchdog restarted the bot (new pid $new_pid)"

    log "post-check: confirming REAL Telegram delivery after restart..."
    if ! send_notify "VERIFY (2/2) — watchdog restarted the bot after crash-test"; then
        err "FAIL: post-check notification not delivered (Telegram API error)"
        return 1
    fi

    log "=== VERIFY PASS ==="
    log "  * bot was killed (pid $old_pid)"
    log "  * watchdog restarted it (pid $new_pid, within ${VERIFY_TIMEOUT}s)"
    log "  * notifier exit code 0 (Telegram API accepted the message) on both sends"
    return 0
}

# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-start}" in
    start|"")
        need_cmd tmux || exit 3
        clean_stale_flags
        if ! termux_wake_lock; then
            err "continuing without wake-lock (notifications/restart may be delayed by Doze)"
        fi
        start_bot || exit 1
        start_watchdog || exit 1
        log "done — attach with: tmux attach -t $BOT_SESSION  (or $WD_SESSION)"
        ;;
    --status|status)
        need_cmd tmux || exit 3
        cmd_status
        ;;
    --stop|stop)
        need_cmd tmux || exit 3
        cmd_stop
        ;;
    --verify|verify)
        need_cmd tmux || exit 3
        cmd_verify
        ;;
    --help|-h|help)
        usage
        ;;
    *)
        err "unknown argument: $1"
        usage
        exit 3
        ;;
esac
