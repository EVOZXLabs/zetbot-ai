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

## Telegram Commands

| Command      | Description                        |
|--------------|------------------------------------|
| `/help`      | List all available commands        |
| `/status`    | Bot status, balance, positions     |
| `/balance`   | Account balance & equity           |
| `/positions` | Show open positions with PnL       |
| `/pipeline`  | Run full analysis pipeline         |
| `/scan`      | Run market scanner only            |
| `/summary`   | Today's trading statistics         |
| `/pause`     | Disable new trade openings         |
| `/resume`    | Enable new trade openings          |

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
