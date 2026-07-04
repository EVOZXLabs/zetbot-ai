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
- Paper Trading
- Live Trading

## Version

v0.1 Complete Paper Trading

## Hotfix — v0.1.4.1

- Fix: duplicate BUY notification guard (`_notified_buy_entry` tracking)
- Fix: state validation now matches actual save format (`paper.*` nesting)
- Fix: integration test no longer blocks on real Telegram API calls
- Regression: repeated cycles never send duplicate BUY notifications
- Regression: repeated cycles never create duplicate BUY positions
- All 345 tests pass (zero failures)

## Notification

- Telegram integration (TelegramNotifier)
- Bot start / stop
- BUY and SELL (TP / SL / Strategy Exit) trade alerts
- Exchange / API errors
- Daily trading summary
- Silent graceful degradation when disabled

## Author

EVOZXLabs
