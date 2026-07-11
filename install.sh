#!/usr/bin/env bash
#
# ZetBot AI — Installer
#
# Automatically installs dependencies, prepares the environment,
# launches the Setup Wizard, and starts ZetBot AI.
#
# Usage:
#   bash install.sh
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "=================================================="
echo "           ZetBot AI — Installer"
echo "=================================================="
echo -e "${NC}"

# --- Python check -------------------------------------------------------
echo -e "${YELLOW}[1/6] Checking Python...${NC}"
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PY_VER=$("$cmd" --version 2>&1 | awk '{print $2}')
        MAJOR=$(echo "$PY_VER" | cut -d. -f1)
        MINOR=$(echo "$PY_VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON="$cmd"
            echo -e "  ${GREEN}Python ${PY_VER} found${NC}"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "  ${RED}Python 3.10+ is required. Please install python3.${NC}"
    exit 1
fi

# --- Create virtual environment (optional) --------------------------------
echo -e "${YELLOW}[2/6] Preparing environment...${NC}"
if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv venv
    echo -e "  ${GREEN}Virtual environment created${NC}"
    source venv/bin/activate
else
    echo "  Using existing virtual environment"
    source venv/bin/activate 2>/dev/null || true
    # If venv activation fails, use system python
fi

# --- Install dependencies ------------------------------------------------
echo -e "${YELLOW}[3/6] Installing dependencies...${NC}"
if [ -f "requirements.txt" ]; then
    "$PYTHON" -m pip install --upgrade pip -q 2>/dev/null || true
    "$PYTHON" -m pip install -r requirements.txt
    echo -e "  ${GREEN}Dependencies installed${NC}"
else
    echo -e "  ${RED}requirements.txt not found${NC}"
    exit 1
fi

# --- Create required folders --------------------------------------------
echo -e "${YELLOW}[4/6] Creating required folders...${NC}"
mkdir -p data logs backups
echo -e "  ${GREEN}Folders created: data/ logs/ backups/${NC}"

# --- Launch Setup Wizard ------------------------------------------------
echo -e "${YELLOW}[5/6] Launching Setup Wizard...${NC}"
echo ""
"$PYTHON" main.py --setup

echo ""
# --- Start Bot ---------------------------------------------------------
echo -e "${YELLOW}[6/6] Starting ZetBot AI...${NC}"
echo ""
echo -e "${GREEN}=================================================="
echo "  ZetBot AI is starting up."
echo "  Press Ctrl+C to stop the bot."
echo -e "==================================================${NC}"
echo ""

"$PYTHON" main.py
