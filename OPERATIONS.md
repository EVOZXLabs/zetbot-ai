# ZetBot AI — Operations Guide

## Overview

ZetBot AI provides a complete suite of operations tools:

| Command | Description |
|---------|-------------|
| `--setup` | First-time Setup Wizard |
| `--config` | Display current configuration |
| `--reset-config` | Reset configuration to defaults |
| `--wizard` | Interactive operations menu |
| `--diagnostics` | Comprehensive system check |
| `--backup` | Create configuration and data backup |
| `--restore <file>` | Restore from backup |
| `--export-config` | Export configuration to JSON |
| `--import-config <file>` | Import configuration from JSON |
| `--test-exchange` | Test exchange API connection |
| `--test-telegram` | Test Telegram connection |
| `--system` | Display system information |

---

## Setup Wizard

```bash
python3 main.py --setup
```

Walks you through all configuration options including:
- Exchange selection
- API credentials
- Trading parameters
- Telegram configuration
- Pipeline settings

The wizard validates all inputs before saving. If `.env` already exists,
you will be asked to confirm overwriting.

---

## Wizard Menu

```bash
python3 main.py --wizard
```

Interactive menu for all operations:

```
  Main Menu:

    1.  Start Bot
    2.  Setup Wizard
    3.  Show Configuration
    4.  Update Configuration
    5.  Test Exchange Connection
    6.  Test Telegram Connection
    7.  Backup
    8.  Restore
    9.  Export Configuration
   10.  Import Configuration
   11.  Diagnostics
   12.  System Information
   13.  Update System

    0.  Exit
```

---

## Diagnostics

```bash
python3 main.py --diagnostics
```

Checks all subsystems:

- **Python**: version check (3.10+ required)
- **Dependencies**: all required packages installed
- **Internet**: DNS resolution and connectivity
- **Configuration**: `.env` file validity
- **Exchange**: API availability, server time, latency
- **Telegram**: credentials and configuration
- **Filesystem**: required folders and data files
- **Logs**: log file availability

Results are displayed with ✅ PASS, ⚠️ WARNING, or ❌ FAIL.

---

## Backup and Restore

### Create Backup

```bash
python3 main.py --backup
```

Creates `backups/backup-YYYYMMDD-HHMMSS.zip` containing:
- `.env` configuration
- `data/` directory (all JSON files)
- `logs/` directory
- Backup manifest (`backup-info.json`)

### List Backups

```bash
ls -la backups/
```

### Restore Backup

```bash
python3 main.py --restore backups/backup-20260101-120000.zip
```

Validates backup integrity before restoring. Existing files are overwritten
after confirmation.

---

## Configuration Import / Export

### Export

```bash
# Standard export (secrets masked)
python3 main.py --export-config

# Include API secrets (unsafe — use with caution)
python3 main.py --export-config --include-secrets

# Password-protected export
python3 main.py --export-config --password mysecret
```

Generates `zetbot-config.json`.

### Import

```bash
# Import configuration
python3 main.py --import-config zetbot-config.json

# Force import (skip validation warnings)
python3 main.py --import-config zetbot-config.json --force

# Import password-protected config
python3 main.py --import-config zetbot-config.json --password mysecret
```

Validates all values before applying. Does not overwrite existing
configuration without confirmation (unless `--force` is used).

---

## Exchange Connection Test

```bash
python3 main.py --test-exchange
```

Tests:
- API status (public endpoint)
- Server time and latency
- Account access (if API key configured)

**Never places orders.**

---

## Telegram Connection Test

```bash
python3 main.py --test-telegram
```

Sends a test message to the configured chat ID:
```
✅ ZetBot AI Connected Successfully
```

---

## System Information

```bash
python3 main.py --system
```

Displays:
- Version and Git commit
- Python version
- Operating system
- CPU info
- Memory (total)
- Disk (total / free)
- Trading mode
- Exchange
- Data directory
- Scheduler status

---

## Update

```bash
bash update.sh
```

The updater:
1. Backs up `.env` and `data/`
2. Pulls latest code from git
3. Installs any new dependencies
4. Restores configuration if new `.env` is missing

Does **not** overwrite your existing `.env` or trading data.

---

## Configuration Reference

All settings are stored in `.env`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `EXCHANGE` | `binance` | Exchange (binance, bybit, tokocrypto) |
| `PAPER_MODE` | `true` | Paper trading (true) or live (false) |
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |
| `POSITION_SIZE` | `10` | Position size in USDT |
| `MAX_POSITIONS` | `1` | Maximum open positions |
| `TIMEFRAME` | `1h` | Trading timeframe |
| `AUTO_PIPELINE` | `true` | Automatic pipeline execution |
| `PIPELINE_INTERVAL` | `300` | Pipeline interval (seconds) |
| `ACCOUNT_BALANCE` | `10000` | Initial balance (USDT) |
| `MAX_RISK_PER_TRADE_PCT` | `2.0` | Max risk per trade (%) |

---

## File Structure

```
./
├── .env                  # Configuration (generated by setup)
├── main.py               # Application entry point
├── install.sh            # Installer script
├── update.sh             # Updater script
├── requirements.txt      # Python dependencies
├── scripts/              # Core modules
│   ├── app_config.py     # Configuration schema
│   ├── config_manager.py # .env management
│   ├── setup_wizard.py   # Interactive setup
│   ├── startup_validator.py  # Pre-flight checks
│   ├── diagnostics.py    # System diagnostics
│   ├── backup_restore.py # Backup/restore
│   ├── config_import_export.py  # Import/export
│   ├── exchange_test.py  # Exchange test
│   ├── telegram_test.py  # Telegram test
│   ├── system_info.py    # System information
│   └── wizard_menu.py    # Interactive menu
├── data/                 # Runtime data
├── logs/                 # Log files
├── backups/              # Backup archives
└── telegram/             # Telegram command system
```

---

## Troubleshooting

### Bot won't start

Run diagnostics:
```bash
python3 main.py --diagnostics
```

### Exchange connection fails

1. Check exchange name is correct (binance, bybit, tokocrypto)
2. Verify API key has correct permissions (read-only is sufficient)
3. Check internet connection
4. Some exchanges block certain regions

### Telegram messages not received

1. Run test: `python3 main.py --test-telegram`
2. Verify bot token from @BotFather
3. Verify chat ID (message @userinfobot)
4. Ensure the bot has started a conversation with your Telegram bot

### Backup fails

1. Check `backups/` directory exists
2. Check disk space (`df -h`)
3. Verify `data/` and `logs/` directories exist

### Update fails

1. Check git remote: `git remote -v`
2. Stash local changes: `git stash`
3. Run update again
