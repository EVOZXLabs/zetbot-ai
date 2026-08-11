#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — Launcher
#
#  Starts the trading bot. On Termux (Android) with tmux installed it starts
#  the supervised setup (bot + watchdog in dedicated tmux sessions, with a
#  wake-lock so the bot survives when the Termux app is minimized). Everywhere
#  else it runs the bot in the foreground.
#
#  This script only *runs* the bot/watchdog. It does NOT touch trading logic.
#
#  Usage:
#      bash run.sh             start the bot
#      bash run.sh --status    (Termux) show tmux sessions / pids / flags
#      bash run.sh --stop      (Termux) stop bot + watchdog
#      bash run.sh --wizard    (foreground) open the operations menu
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BOLD=''; NC=''
fi

has_cmd() { command -v "$1" >/dev/null 2>&1; }

is_termux() {
    [[ "${PREFIX:-}" == *"/com.termux"* ]] && return 0
    [[ -d /data/data/com.termux ]] && return 0
    return 1
}

# ── .env must exist before the bot starts ──────────────────────────────────
if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
        cp .env.example .env
        chmod 600 .env 2>/dev/null || true
        echo -e "${YELLOW}No .env found — created one from .env.example.${NC}"
        echo -e "${YELLOW}Edit credentials later with: nano .env${NC}"
    else
        echo -e "${RED}Neither .env nor .env.example found — run: bash install.sh${NC}"
        exit 1
    fi
fi

# ── Python (prefer the virtualenv created by install.sh) ───────────────────
PY="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    PY="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$PY" ]]; then
    echo -e "${RED}Python not found — run: bash install.sh${NC}"
    exit 1
fi

# ── Termux: supervised start (bot + watchdog in tmux) ──────────────────────
if is_termux && has_cmd tmux && [[ -x scripts/termux-start.sh ]]; then
    exec bash scripts/termux-start.sh "${@:-start}"
fi

# ── Foreground start (VPS / desktop / no tmux) ─────────────────────────────
exec "$PY" main.py "$@"
