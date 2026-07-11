# ZetBot AI — Installation Guide

## Quick Install (Ubuntu / Debian)

```bash
# Clone the repository
git clone https://github.com/anomalyco/zetbot-ai.git
cd zetbot-ai

# Run the installer
bash install.sh
```

The installer will:
1. Check Python 3.10+
2. Create a virtual environment
3. Install all dependencies
4. Create required folders (`data/`, `logs/`, `backups/`)
5. Launch the Setup Wizard
6. Start ZetBot AI

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
git clone https://github.com/anomalyco/zetbot-ai.git
cd zetbot-ai

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

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
pkg install -y python git clang libffi openssl

# Clone the repository
git clone https://github.com/anomalyco/zetbot-ai.git
cd zetbot-ai

# Install dependencies
pip install -r requirements.txt

# Create required folders
mkdir -p data logs backups

# Run Setup Wizard
python3 main.py --setup

# Start the bot
python3 main.py
```

### VPS (Any Linux)

```bash
# SSH into your VPS
ssh user@your-vps-ip

# Install Python and Git
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repository
git clone https://github.com/anomalyco/zetbot-ai.git
cd zetbot-ai

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

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
```

### "pip install fails"

```bash
# On Termux
pkg install -y python python-pip clang libffi openssl

# On Ubuntu
pip install --upgrade pip setuptools wheel
```

### "Module not found"

```bash
pip install -r requirements.txt
```

### "Connection refused" (exchange)

Your exchange might be restricted in your region. Try a different exchange or use a VPN.
