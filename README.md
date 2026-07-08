# ZetBot AI

Professional Spot Trading Bot for:

- Binance
- Bybit
- Tokocrypto

## Features

- EMA 200
- RSI 14
- ADX Filter
- Sideways Detection
- Position Sizing
- Stop Loss
- Take Profit
- Telegram Notification
- Telegram Command Center (/status, /balance, /pipeline, /pause, etc.)
- Paper Trading
- Live Trading

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your exchange API keys and Telegram credentials

# Start ZetBot (single command — Telegram Command Center starts automatically)
python main.py
```

That's it. The pipeline runs once on startup, then the Telegram Command Center
stays active in the background. Send `/help` to your bot to see all commands.

No second terminal, no `nohup`, no `tmux`, no `screen` required.

## Architecture

```
telegram/
├── command_center.py    # Dispatcher: parse → middleware → execute → reply
├── registry.py          # Auto-discovers commands in telegram/commands/
├── context.py           # Per-request context (config, exchange, logger, ...)
├── middleware.py        # Auth, cooldown, exception handling pipeline
├── permissions.py       # Chat authorization
├── formatter.py         # Markdown formatting helpers
├── base_command.py      # Abstract base class for all commands
└── commands/            # One file per command — drop a file, it's registered
    ├── status.py        # /status — bot status overview
    ├── balance.py       # /balance — account balance & PnL
    ├── positions.py     # /positions — open positions with SL/TP
    ├── pipeline.py      # /pipeline — run analysis pipeline
    ├── scan.py          # /scan — run scanner only
    ├── summary.py       # /summary — trading statistics
    ├── health.py        # /health — system health overview
    ├── version.py       # /version — ZetBot version info
    ├── pause.py         # /pause — disable new trades
    ├── resume.py        # /resume — enable new trades
    ├── shutdown.py      # /shutdown — graceful shutdown
    ├── help.py          # /help — list all commands
    ├── logs.py          # /logs — recent log output
    ├── config.py        # /config — show configuration
    ├── wallet.py        # /wallet — wallet summary
    └── ...              # Extend by adding one file
```

### Adding a New Command

Create one file in `telegram/commands/`:

```python
from telegram.base_command import BaseCommand, CommandMeta

class MyCommand(BaseCommand):
    meta = CommandMeta(
        name="mycommand",
        aliases=["mc"],
        description="Does something useful",
        usage="/mycommand [arg]",
        permission="user",   # or "admin"
    )
    def execute(self, ctx, args: str) -> str:
        return "Hello from my command!"
```

That's it. No imports, no registration, no `if/elif`. The registry auto-discovers it.

## Telegram Commands

| Command        | Description                        |
|----------------|------------------------------------|
| `/help`        | List all available commands        |
| `/start`       | Alias for /help                    |
| `/status`      | Bot status, balance, positions     |
| `/balance`     | Account balance & equity           |
| `/positions`   | Show open positions with PnL       |
| `/pipeline`    | Run full analysis pipeline         |
| `/scan`        | Run market scanner only            |
| `/summary`     | Today's trading statistics         |
| `/health`      | System health & component status   |
| `/version`     | Bot version & system info          |
| `/pause`       | Disable new trade openings         |
| `/resume`      | Enable new trade openings          |
| `/shutdown`    | Gracefully shut down the bot       |
| `/logs`        | Show recent log output             |

## Configuration

All settings via environment variables (`.env`):

| Variable             | Default    | Description                   |
|----------------------|------------|-------------------------------|
| `EXCHANGE`           | binance    | Exchange name                 |
| `TIMEFRAME`          | 1h         | Trading timeframe             |
| `PAPER_MODE`         | true       | Paper trading when true       |
| `TELEGRAM_ENABLED`   | false      | Enable Telegram integration   |
| `TELEGRAM_TOKEN`     |            | Bot token from BotFather      |
| `TELEGRAM_CHAT_ID`   |            | Your Telegram chat ID         |
| `ACCOUNT_BALANCE`    | 10000      | Starting paper balance        |

See `.env.example` for the full list.

## Version

v0.4.0 — Integrated Telegram Command Center

## Author

EVOZXLabs
