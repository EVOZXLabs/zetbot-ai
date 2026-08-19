# ZetBot AI — Installation Guide

## One-Click Install (recommended)

After cloning the repository, run exactly **one command**:

```bash
git clone https://github.com/EVOZXLabs/zetbot-ai.git
cd zetbot-ai
bash install.sh
```

Works on **Termux (Android)**, Debian/Ubuntu, and macOS. No manual input is
required during installation, and it is **safe to run again** (idempotent).

System-package operations are fully non-interactive: the installer exports
`DEBIAN_FRONTEND=noninteractive` and passes `Dpkg::Options::=--force-confold`
to every `pkg`/`apt-get` call, so it never blocks on a conffile prompt
(`Y/I/N/O/D/Z`) when run via `curl … | bash` or `bash install.sh`. Existing
configuration files on the device are preserved (never overwritten).

The installer will:

1. Detect the platform (Termux is checked first)
2. Update system packages (`pkg update && pkg upgrade` on Termux)
3. Install required system packages:
   `git`, `python`, `clang`, `rust`, `openssl`, `libffi` (Termux names)
4. Create a virtual environment (`.venv/`)
5. Install `requirements.txt` into the virtual environment
6. Create `.env` from `.env.example` (an existing `.env` is never overwritten)
7. Create the required folders (`data/`, `logs/`, `backups/`)
8. Run a self-check and show a clear PASS/FAIL summary

### Commands after installation

| Command | What it does |
| --- | --- |
| `bash install.sh` | Install / repair the environment (safe to re-run) |
| `bash run.sh` | Start the bot (Termux: supervised in tmux) |
| `bash update.sh` | Pull the latest code + update dependencies |
| `bash uninstall.sh` | Remove the bot (your config + data are preserved) |

To start the bot for the first time:

```bash
bash run.sh
```

The bot runs in **paper trading mode by default** (`PAPER_MODE=true`) — no real
funds are at risk until you edit `.env`. Configure credentials with:

```bash
nano .env
```

---

## Manual Installation

### Prerequisites

- **Python 3.10+** ([python.org](https://python.org))
- **Git** ([git-scm.com](https://git-scm.com))
- **pip** (comes with Python 3.4+)

### Ubuntu / Debian

```bash
# Install Python and Git
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repository
git clone https://github.com/EVOZXLabs/zetbot-ai.git
cd zetbot-ai

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create required folders
mkdir -p data logs backups

# Run Setup Wizard
python3 main.py --setup

# Start the bot
python3 main.py
```

### Termux (Android)

```bash
# Install Termux from F-Droid (not Google Play)
pkg update && pkg upgrade
pkg install -y python git clang rust openssl libffi

# Clone the repository
git clone https://github.com/EVOZXLabs/zetbot-ai.git
cd zetbot-ai

# Install dependencies
pip install -r requirements.txt

# Create required folders
mkdir -p data logs backups

# Run Setup Wizard
python3 main.py --setup

# Start the bot (recommended: tmux so it survives when Termux is minimized)
bash run.sh
```

### VPS (Any Linux)

```bash
# SSH into your VPS
ssh user@your-vps-ip

# Install Python and Git
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repository
git clone https://github.com/EVOZXLabs/zetbot-ai.git
cd zetbot-ai

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create required folders
mkdir -p data logs backups

# Run Setup Wizard
python3 main.py --setup

# Start the bot (use tmux or screen for persistent sessions)
tmux new -s zetbot
python3 main.py
# Detach: Ctrl+B, D
# Reattach: tmux attach -t zetbot
```

---

## Post-Installation

After installation, verify everything works:

```bash
# Run diagnostics
python3 main.py --diagnostics

# Test exchange connection
python3 main.py --test-exchange

# Test Telegram (if configured)
python3 main.py --test-telegram

# View system information
python3 main.py --system

# Open the operations menu
python3 main.py --wizard
```

---

## Troubleshooting

### "Python 3.10+ is required"

Install a newer Python version:

```bash
# Ubuntu / Debian
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt install -y python3.11 python3.11-venv

# Termux
pkg install -y python
```

### "pip install fails"

```bash
# On Termux
pkg install -y python clang rust openssl libffi

# On Ubuntu
pip install --upgrade pip setuptools wheel
```

### "Module not found"

```bash
pip install -r requirements.txt
```

### "Connection refused" (exchange)

Your exchange might be restricted in your region. Try a different exchange or use a VPN.
