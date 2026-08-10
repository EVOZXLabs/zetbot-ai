#!/usr/bin/env bash
#
# ZetBot AI — Updater
#
# Pulls the latest version from git, installs new dependencies,
# verifies configuration, and preserves user data.
#
# Usage:
#   bash update.sh
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "=================================================="
echo "           ZetBot AI — Updater"
echo "=================================================="
echo -e "${NC}"

# --- Check git availability ---------------------------------------------
echo -e "${YELLOW}[1/5] Checking git...${NC}"
if ! command -v git &>/dev/null; then
    echo -e "  ${RED}git is not installed.${NC}"
    exit 1
fi

if [ ! -d ".git" ]; then
    echo -e "  ${RED}Not a git repository. Please clone from origin.${NC}"
    exit 1
fi
echo -e "  ${GREEN}git available${NC}"

# --- Backup current configuration ---------------------------------------
echo -e "${YELLOW}[2/5] Backing up configuration...${NC}"
if [ -f ".env" ]; then
    cp ".env" ".env.update-backup"
    echo -e "  ${GREEN}.env backed up to .env.update-backup${NC}"
else
    echo -e "  ${YELLOW}No .env to back up${NC}"
fi

# --- Backup user data ---------------------------------------------------
echo -e "${YELLOW}[3/5] Preserving user data...${NC}"
DATA_BACKUP=".data-update-backup"
if [ -d "data" ]; then
    mkdir -p "$DATA_BACKUP"
    cp -r data/* "$DATA_BACKUP/" 2>/dev/null || true
    echo -e "  ${GREEN}Data preserved in ${DATA_BACKUP}/${NC}"
else
    echo -e "  ${YELLOW}No data directory to back up${NC}"
fi

# --- Pull latest code ---------------------------------------------------
echo -e "${YELLOW}[4/5] Pulling latest version...${NC}"
if git pull; then
    echo -e "  ${GREEN}Repository updated${NC}"
else
    echo -e "  ${RED}git pull failed. Check your network connection.${NC}"
    exit 1
fi

# --- Install any new dependencies ---------------------------------------
echo -e "${YELLOW}[5/5] Installing new dependencies...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=""
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
    echo -e "  ${DIM}Using virtualenv: ${SCRIPT_DIR}/.venv${NC}"
else
    if command -v python3 &>/dev/null; then
        PYTHON="python3"
    elif command -v python &>/dev/null; then
        PYTHON="python"
    fi
fi

if [ -f "requirements.txt" ] && [ -n "$PYTHON" ]; then
    $PYTHON -m pip install --upgrade pip -q 2>/dev/null || true
    $PYTHON -m pip install -r requirements.txt
    echo -e "  ${GREEN}Dependencies updated${NC}"
fi

# --- Restore configuration if new .env is missing ------------------------
if [ ! -f ".env" ] && [ -f ".env.update-backup" ]; then
    cp ".env.update-backup" ".env"
    echo -e "  ${YELLOW}Configuration restored from backup${NC}"
fi

echo ""
echo -e "${GREEN}=================================================="
echo "  Update complete!"
echo ""
echo "  To verify: python3 main.py --diagnostics"
echo "  To start:  python3 main.py"
echo "  To open menu: python3 main.py --wizard"
echo -e "==================================================${NC}"
