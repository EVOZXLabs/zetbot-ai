#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — Uninstaller
#
#  Safely removes the bot from the machine:
#    1. Stops the running bot + watchdog (if any)
#    2. Preserves your configuration and trading data in a timestamped
#       backup directory (".uninstall-backup-<timestamp>/") — NEVER deletes
#       .env or data/ directly
#    3. Removes generated artifacts only: virtualenv, logs, backups, caches
#
#  The source code and the .git folder are kept (reinstall = just
#  "bash install.sh" again; restore your old config from the backup).
#
#  Safe to run multiple times (idempotent). No manual input required.
#  Does NOT touch trading logic.
#
#  Usage:
#      bash uninstall.sh
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; DIM=''; NC=''
fi

step() {
    echo ""
    echo -e "${BOLD}[$1/$2] $3${NC}"
}

pass() { echo -e "  ${GREEN}PASS${NC}  $1"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; }
info() { echo -e "  ${DIM}$1${NC}"; }

echo -e "${CYAN}${BOLD}"
echo "  =============================================="
echo "        ZetBot AI — Uninstaller"
echo "  =============================================="
echo -e "${NC}"

# ── 1. Stop bot + watchdog ────────────────────────────────────────────────
step 1 4 "Stopping the bot and watchdog"
if [[ -x scripts/termux-start.sh ]]; then
    bash scripts/termux-start.sh --stop 2>/dev/null || true
fi
pkill -f "main\.py" 2>/dev/null || true
pkill -f "scripts/watchdog\.py" 2>/dev/null || true
pass "Stopped running bot/watchdog (if any)"

# ── 2. Preserve config + trading data ──────────────────────────────────────
step 2 4 "Preserving configuration and trading data"
TS="$(date +%Y%m%d-%H%M%S)"
BK=".uninstall-backup-${TS}"
mkdir -p "$BK"
if [[ -f .env ]]; then
    mv .env "$BK/.env"
    pass ".env preserved in ${BK}/"
else
    info "No .env to preserve"
fi
if [[ -d data ]]; then
    mv data "$BK/data"
    pass "data/ preserved in ${BK}/"
else
    info "No data/ to preserve"
fi

# ── 3. Remove generated artifacts ──────────────────────────────────────────
step 3 4 "Removing generated artifacts"
rm -rf .venv venv __pycache__ .pytest_cache logs backups \
    .env.update-backup .data-update-backup
find . -path "./.git" -prune -o -type d -name "__pycache__" -print \
    -exec rm -rf {} + 2>/dev/null || true
pass "Removed .venv/ venv/ logs/ backups/ and Python caches"

# ── 4. Summary ─────────────────────────────────────────────────────────────
step 4 4 "Summary"
echo -e "  ${GREEN}${BOLD}  UNINSTALL: DONE${NC}"
echo ""
echo -e "  ${BOLD}Removed:${NC} virtualenv, logs, backups, caches"
echo -e "  ${BOLD}Kept:${NC}    source code + .git (reinstall: bash install.sh)"
echo -e "  ${BOLD}Backup:${NC}  your .env and data/ are safe in: ${BK}/"
echo ""
echo -e "  To restore your old config before a reinstall, copy them back:"
echo -e "    cp \"${BK}/.env\" .env   (and)   cp -r \"${BK}/data\" data"
echo ""
exit 0
