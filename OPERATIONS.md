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

## Watchdog / Auto-restart (Recommended for Live Trading)

> ⚠️ **SL/TP protection on exchanges without native stop orders (e.g. indodax)
> only exists while the bot process is alive.** The bot executes SL/TP through
> its own position-monitoring loop — not via exchange stop orders. If the bot
> crashes and stays down, an open position is completely unprotected.
>
> The watchdog is therefore a **critical part of live trading on such
> exchanges, not optional** — it restarts the bot within ~20 s of any crash.

### What it does

- Supervises the bot every `WATCHDOG_INTERVAL` seconds (default `20`).
- If the bot is already running (e.g. started manually in tmux), it attaches
  to that instance instead of starting a second one.
- On crash or exit it restarts the bot with the same command used for a
  manual start (`.venv/bin/python main.py`).
- Does **not** auto-restart on a deliberate stop: `data/.shutdown_requested`
  (from `/shutdown`), a clean exit (code 0), or while `data/.watchdog_paused`
  exists.
- Rate-limits crash loops: more than `WATCHDOG_MAX_RESTARTS` (default `3`)
  crashes inside `WATCHDOG_WINDOW` (default `600`) seconds halts auto-restart,
  writes `data/.watchdog_halt`, alerts via Telegram ("MANUAL INTERVENTION
  NEEDED") and exits non-zero. The halt is sticky — the watchdog keeps
  standing by until you remove `data/.watchdog_halt`.
- Sends Telegram alerts on restart / halt / manual stop, using the same
  `TELEGRAM_*` settings as the bot.

### Run it in the foreground (tmux / screen / termux-services)

```bash
cd /path/to/zetbot-ai
./.venv/bin/python scripts/watchdog.py
```

Run it under `tmux` / `screen`, or use Termux services on Android
(`sv-enable zetbot-watchdog`), so it survives the terminal closing.

### Run it under systemd (VPS)

```bash
sudo cp deploy/zetbot-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zetbot-watchdog
```

Adjust `User`, `WorkingDirectory` and `ExecStart` in
`deploy/zetbot-watchdog.service` to match your install. The unit uses
`Restart=always` and `KillMode=process` (stopping the service leaves the bot
running).

### Controlling the watchdog

| Command | Effect |
|---|---|
| `python scripts/watchdog.py --status` | Show bot / watchdog / flag state |
| `python scripts/watchdog.py --stop` | Stop the watchdog (bot keeps running) |
| `touch data/.watchdog_paused` | Pause auto-restart (bot unaffected) |
| `rm data/.watchdog_halt` | Re-arm the watchdog after a halt (fix the bug first) |
| `rm data/.shutdown_requested` | Let the watchdog (re)start the bot after a deliberate stop |

### Troubleshooting

- **Bot keeps dying and the watchdog halts**: inspect `logs/bot-console.log`
  and `logs/watchdog.log`, fix the bug, then `rm data/.watchdog_halt` and
  restart the watchdog.
- **Bot won't start after a `/shutdown`**: a stale `data/.shutdown_requested`
  is respected as a deliberate stop. Remove it (`rm data/.shutdown_requested`)
  to let the watchdog start the bot again.
- **Status shows `watchdog: alive=False`**: the watchdog is not running —
  start it again (see above). While it is down there is no auto-restart.

---

## Menjalankan di Termux (Android)

Termux has **no systemd**, so the bot + watchdog are kept alive with `tmux`
sessions (and optionally the Termux:Boot app after reboot). Everything below
is an **operational layer on top of the bot/watchdog** — no trading or
watchdog logic is modified.

### 1. Install prerequisites

```bash
pkg update
pkg install tmux termux-api
```

`termux-api` provides `termux-wake-lock` (keeps the CPU awake so the bot and
watchdog survive Android's Doze). For the wake-lock to actually work you must
also install the **Termux:API app** (separate from the package):

- F-Droid: `https://f-droid.org/packages/com.termux.api/`
- (Play Store also has it, but F-Droid is the maintained source)

Install it, open it once, then re-run the launcher.

### 2. Manual start (tmux)

```bash
cd /path/to/zetbot-ai

# Session 1 — the bot
tmux new-session -d -s zetbot-bot 'python main.py'

# Session 2 — the watchdog (attaches to the running bot)
tmux new-session -d -s zetbot-watchdog 'python scripts/watchdog.py'
```

View/attach: `tmux attach -t zetbot-bot` (detach with `Ctrl-b d`).
The watchdog attaches to the bot via `data/zetbot.pid`; if the bot crashes,
the watchdog restarts it as its own child.

### 3. Automatic start (recommended) — `termux-start.sh`

```bash
./scripts/termux-start.sh            # start bot + watchdog (idempotent)
./scripts/termux-start.sh --status   # sessions / pids / flags / logs
./scripts/termux-start.sh --verify   # crash-test: kill bot, watchdog restart,
                                     #   real Telegram delivery check
./scripts/termux-start.sh --stop     # stop watchdog then bot (graceful)
```

The launcher:

- takes `termux-wake-lock` (and prints install instructions if Termux:API is
  missing),
- cleans stale flags (`data/.shutdown_requested`, `data/.watchdog_halt`,
  `data/.watchdog_paused`) **only if they are older than 5 minutes**
  (`ZETBOT_STALE_FLAG_MINUTES`) — a halt/pause flag an operator just created
  is respected and kept,
- starts bot + watchdog in separate tmux sessions only if they are not
  already running (safe to re-run any time — no duplicate sessions).

### 4. Auto-start on reboot — Termux:Boot

Termux:Boot (`https://f-droid.org/packages/com.termux.boot/`) runs scripts in
`~/.termux/boot/` after every reboot:

```bash
mkdir -p ~/.termux/boot
cp scripts/termux-boot/zetbot-start.sh ~/.termux/boot/
chmod +x ~/.termux/boot/zetbot-start.sh
```

It waits for the device/network to settle, then calls `termux-start.sh`
(idempotent) and logs to `~/zetbot-boot.log`. See
`scripts/termux-boot/README.md`.

### 5. Battery optimization (READ THIS — most common cause of a "stopped" watchdog)

Android aggressively freezes background apps (Doze / App Standby). Termux is
an ordinary app, so **Android can silently freeze the entire Termux process —
and the watchdog inside it — with no crash, no log, no error.** The code can
be perfectly correct and the watchdog still "stops for no reason".

Whitelist both apps from battery optimization:

- **Termux** and **Termux:Boot** (and Termux:API if you use it):

  1. Open Android **Settings → Apps** (or **App info**).
  2. Tap **Termux** → **Battery** (wording varies by device).
  3. Choose **Unrestricted** (or "Don't optimize" / "No restrictions").
     "Optimized" or "Restricted" is NOT enough for a 24/7 bot.
  4. Repeat for **Termux:Boot**.

- OEM-specific notes (the menu names differ):
  - **Xiaomi/POCO (MIUI/HyperOS)**: Settings → Apps → Manage apps →
    Termux → Battery saver → **No restrictions**. Also disable "Pause app
    activity if unused". MIUI can kill apps even when they're in the
    foreground of another app.
  - **Samsung (One UI)**: Settings → Battery → Background usage limits →
    select Termux → **Never sleeping apps**. Also under "App settings" →
    **Allow background activity**.
  - **Oppo/Realme (ColorOS)**: Settings → Battery → App battery management →
    Termux → **Allow auto-launch** + **Allow background running**.
  - **Huawei/Honor (EMUI/HarmonyOS)**: Settings → Battery → App launch →
    Termux → toggle all three to **Manage manually** and enable everything.

- Extra: with `termux-wake-lock` active the CPU stays awake while Termux is
  alive, but it does **not** stop Android from killing the Termux *app*.
  Whitelisting is what prevents that.

After whitelisting, reboot once and confirm with:

```bash
./scripts/termux-start.sh --status
```

### 6. Termux troubleshooting

- **`pkg` says "Cannot run as root"**: Termux refuses root. Run everything
  as the normal Termux user, not via `su`.
- **`termux-wake-lock` is a no-op**: the Termux:API *app* is missing or was
  never opened. Install/open it, then re-run.
- **Watchdog stops without any log entry**: almost always battery
  optimization freezing Termux — see section 5.
- **No notification after a crash**: run `./scripts/termux-start.sh --verify`
  — it reports whether Telegram delivery really succeeds (`NOTIFIER_OK=True`
  means the Telegram API accepted the message; `NOTIFIER_DISABLED` means
  `.env` has `TELEGRAM_ENABLED=false` or missing credentials).
- **Bot started but Telegram command center doesn't answer**: check that the
  bot process is the one started with `main.py` and that `data/zetbot.pid`
  points to a live process.

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
| `WATCHDOG_INTERVAL` | `20` | Watchdog supervision interval (s) |
| `WATCHDOG_MAX_RESTARTS` | `3` | Max bot restarts in `WATCHDOG_WINDOW` before auto-restart halts |
| `WATCHDOG_WINDOW` | `600` | Rate-limit window for watchdog restarts (s) |

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
│   ├── wizard_menu.py    # Interactive menu
│   ├── watchdog.py       # Auto-restart supervisor
│   ├── termux-start.sh   # Termux launcher (tmux + wake-lock + --verify)
│   └── termux-boot/      # Termux:Boot auto-start hook + README
├── deploy/
│   └── zetbot-watchdog.service  # systemd unit for the watchdog
├── data/                 # Runtime data
├── logs/                 # Log files (incl. watchdog.log, bot-console.log)
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
