# ZetBot AI

Professional AI-powered Spot Trading Bot.

ZetBot AI is an automated cryptocurrency spot trading system designed for safe, systematic, and transparent trading.

Supported Exchanges:

- Binance
- Bybit
- Tokocrypto
- OKX
- Gate
- Kucoin
- MEXC
- Indodax (IDR-quoted pairs — set `QUOTE_CURRENCY=IDR`)

---

> ## 👋 Baru di sini? Pengguna HP Android?
>
> Baca **[Panduan Instalasi 5 Menit (QUICKSTART.md)](QUICKSTART.md)** —
> panduan langkah demi langkah untuk **Termux**, tanpa laptop dan tanpa
> perlu tahu Python. Semua cukup disalin-tempel: `bash install.sh` →
> `bash run.sh`.

---

# Features

## Trading Engine

- EMA 200 Trend Filter
- RSI 14 Momentum Filter
- ADX Trend Strength Filter
- Sideways Market Detection
- ATR Volatility Filter
- Trend Following Strategy
- Automated Risk Management
- Position Sizing
- Stop Loss Management
- Take Profit Management
- Paper Trading Mode
- Live Trading Mode

---

## Execution & Accounting

- Paper Trading Engine
- Order Management
- Position Management
- Balance Tracking
- Realized PnL Tracking
- Unrealized PnL Tracking
- Accounting Reconciliation
- Position State Recovery
- State Synchronization Protection
- Trade History Tracking

---

# AI Decision Pipeline

ZetBot uses a multi-stage decision pipeline:

```
Scanner
   ↓
Decision
   ↓
Risk
   ↓
Trade
   ↓
Position
   ↓
Paper Execution
```

Each trade passes through:

### Scanner

Market scanning and candidate selection.

### Decision

Evaluates:

- Probability score
- Trading signal
- Market condition
- Entry quality

### Risk

Checks:

- Risk/reward ratio
- Volatility
- Position limits
- Safety rules

### Trade

Creates and validates trade execution.

### Position

Manages:

- Open positions
- Exit conditions
- Position lifecycle

### Paper Execution

Simulates execution without real funds.

---

# Decision Trace

Decision Trace provides visibility into the complete AI decision process.

It shows:

- Selected candidate
- Scanner result
- Decision result
- Risk approval/rejection
- Execution status
- Final position state

Telegram command:

```
/trace
```

---

# Telegram Command Center

ZetBot includes an integrated Telegram control interface.

No additional terminal session is required.

The Telegram Command Center runs automatically with the bot.

Available commands:

| Command | Description |
|---|---|
| `/help` | List available commands |
| `/status` | Bot status overview |
| `/balance` | Account balance and PnL |
| `/wallet` | Wallet summary |
| `/positions` | Open positions |
| `/pipeline` | Run complete pipeline |
| `/scan` | Run market scanner |
| `/summary` | Trading statistics |
| `/health` | System health information |
| `/trace` | AI decision trace |
| `/version` | Version information |
| `/pause` | Disable new trades |
| `/resume` | Enable new trades |
| `/shutdown` | Graceful shutdown |
| `/logs` | View recent logs |
| `/config` | Current configuration |

---

# Quick Start

> **On a phone (Termux)?** Follow the 5-minute guide:
> **[QUICKSTART.md](QUICKSTART.md)** — install, configure, run, stop,
> update, logs, and uninstall with copy-paste commands.

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configure Environment

Create configuration file:

```bash
cp .env.example .env
```

Edit `.env`:

```env
EXCHANGE=binance
TIMEFRAME=1h

PAPER_MODE=true

ACCOUNT_BALANCE=10000

TELEGRAM_ENABLED=true
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## API Key Security

**Required permissions (trading-only key):**

| Permission | Required | Notes |
|---|---|---|
| Read / View | ✅ Yes | Balance, positions, order status |
| Spot Trading | ✅ Yes | Place and cancel orders |
| Withdrawal | ❌ **Never** | Must be disabled — a leaked key cannot drain funds |
| Margin / Futures | ❌ Never | ZetBot is spot-only |
| Transfer | ❌ Never | Not needed |

Never create an API key with Withdrawal permission.
Even a leaked key can only trade — it cannot move funds out of the exchange.

**Credential safety:**

- `.env` is in `.gitignore` — credentials are never committed.
- Use `.env.example` as the template; never put real values in it.
- Rotate your API key immediately if you suspect a leak.
- Store the raw key+secret only in `.env` on the server — never in code, logs, or Telegram.

---

## Production Health Check

Run:

```bash
./setup.sh
```

The setup checker validates:

- Python environment
- Virtual environment
- Dependencies
- Configuration
- Exchange connectivity
- Telegram configuration
- Runtime directories
- Git status

To test connectivity against whichever exchange is set in `.env`
(`EXCHANGE=...`) specifically:

```bash
python main.py --test-exchange
```

---

## Start ZetBot AI

Run:

```bash
python main.py
```

The bot automatically starts:

- Trading Engine
- Pipeline Scheduler
- Telegram Command Center
- Position Monitoring
- Accounting System
- Health Monitor

No:

- nohup
- tmux
- screen

required.

### Keep the bot alive (live trading)

SL/TP protection on exchanges without native stop orders (e.g. indodax) only
exists while the bot process is running — a dead bot means an unprotected live
position. Use the bundled watchdog (`scripts/watchdog.py`) to auto-restart the
bot within ~20 s of any crash:

```bash
./.venv/bin/python scripts/watchdog.py
```

See the **Watchdog / Auto-restart** section in `OPERATIONS.md` (including the
systemd unit `deploy/zetbot-watchdog.service`) for full instructions and the
crash-loop safety rules.

---

# Architecture

```
ZetBot AI

main.py
 |
 ├── Trading Engine
 |
 ├── Pipeline
 |     |
 |     ├── Scanner
 |     ├── Decision
 |     ├── Risk
 |     ├── Trade
 |     ├── Position
 |     └── Paper
 |
 ├── Accounting System
 |
 ├── Position Manager
 |
 ├── Notifier
 |
 ├── Health Monitor
 |
 └── Telegram Command Center
```

---

# Telegram Architecture

```
telegram/

├── command_center.py
├── registry.py
├── context.py
├── middleware.py
├── permissions.py
├── formatter.py
├── base_command.py

└── commands/

    ├── status.py
    ├── balance.py
    ├── wallet.py
    ├── positions.py
    ├── pipeline.py
    ├── scan.py
    ├── summary.py
    ├── health.py
    ├── trace.py
    ├── version.py
    ├── pause.py
    ├── resume.py
    ├── shutdown.py
    ├── logs.py
    ├── config.py
    └── help.py
```

Commands are automatically discovered.

Adding a new command only requires creating a new command module.

---

# Configuration

All settings are managed through `.env`.

| Variable | Default | Description |
|---|---|---|
| `EXCHANGE` | binance | Exchange provider (binance, bybit, tokocrypto, okx, gate, kucoin, mexc, indodax) |
| `QUOTE_CURRENCY` | USDT | Quote currency to scan/trade against (e.g. IDR for Indodax) |
| `TIMEFRAME` | 1h | Trading timeframe |
| `PAPER_MODE` | true | Enable paper trading |
| `ACCOUNT_BALANCE` | 10000 | Starting paper balance |
| `TELEGRAM_ENABLED` | false | Enable Telegram |
| `TELEGRAM_TOKEN` | | Telegram bot token |
| `TELEGRAM_CHAT_ID` | | Authorized Telegram ID |

See `.env.example` for complete configuration.

---

# Runtime Safety

ZetBot includes:

- PID locking
- Health monitoring
- Automatic recovery
- Accounting reconciliation
- Position synchronization
- Notification retry handling
- Error isolation

Runtime directories:

```
data/
logs/
backups/
```

are excluded from Git tracking.

---

# Development

Run tests:

```bash
pytest
```

Validation includes:

- Accounting consistency
- Paper trading validation
- Position synchronization
- Service container testing
- Production regression testing
- Multi-exchange provider & scanner coverage (`tests/test_exchange_providers.py`, `tests/test_scanner.py`, `tests/test_data.py`)

---

# Future Expansion

ZetBot AI is designed with future Web3 integration in mind.

Planned expansion:

## Blockchain Trading

Potential future support:

- DEX trading integration
- On-chain market analysis
- Wallet-based portfolio management
- Smart contract interaction
- Web3 liquidity monitoring
- Cross-chain market intelligence

## AI Crypto Assistant

Future capabilities:

- Conversational trading assistant
- Market analysis through AI
- Portfolio insights
- Trading strategy assistance
- Web3 ecosystem monitoring

Blockchain trading features will be introduced after the core centralized exchange trading engine reaches production maturity.

---

# Version

```
v0.5.1

Multi-Exchange Scanner Fix (Fase 0 — Web3/DEX Roadmap)
Previously the scanner always scanned Binance/USDT regardless of the
EXCHANGE/QUOTE_CURRENCY settings in .env. Scanner, MarketData, and the
exchange diagnostics tools now correctly follow the configured exchange
and quote currency across all 8 supported exchanges. See
FASE0-SUMMARY.md for details.

v0.5.0

Decision Trace & Paper Trading Stability Update
```

---

# Author

EVOZXLabs
