# ZetBot AI Specification

Version: 1.0

Status: LOCKED

Owner: EVOZXLabs

Document Type: Project Contract

Last Updated: 2026

---

# 1. Mission

Develop a professional cryptocurrency Spot Trading Bot that is:

- Safe
- Modular
- Maintainable
- Testable
- Production Ready

The bot must be suitable for real trading after passing paper trading and backtesting.

This document is the single source of truth.

All implementations MUST follow this specification.

---

# 2. Core Principles

The project MUST follow these principles.

- Build incrementally.
- Every milestone must be executable.
- Never leave broken code.
- Never create placeholder implementations.
- Never create unused modules.
- Never refactor architecture before Version 1.0.
- Never implement future milestones.
- One feature = One commit.
- One milestone = Working software.

---

# 3. Supported Exchanges

Required

- Binance Spot
- Bybit Spot
- Tokocrypto Spot

Implementation must use:

CCXT

The architecture must support adding new exchanges with minimal code changes.

---

# 4. Trading Mode

Supported

- Paper Trading
- Live Trading

Forbidden

- Margin
- Futures
- Leverage

Spot Trading ONLY.

---

# 5. Trading Strategy

Primary Strategy

Trend Following with RSI Filter

BUY Conditions

ALL conditions must be TRUE.

- Current Price > EMA 200
- RSI(14) < 30
- ADX >= Configurable Threshold
- Market is NOT Sideways
- Volume Filter passed
- No Active Position
- Balance Available
- Daily Loss Limit NOT reached
- Maximum Daily Trade NOT reached

SELL Conditions

Any of the following:

- Take Profit reached
- Stop Loss reached
- Exit Strategy triggered
- Emergency Exit triggered

---

# 6. Indicators

Required

- EMA 200
- RSI 14
- ADX 14
- ATR
- Volume Filter

Future

- MACD
- Bollinger Bands
- Multi-Timeframe Confirmation

Indicators must never repaint.

Only closed candles may be used.

---

# 7. Sideways Detection

The bot MUST avoid trading during ranging markets.

Possible methods

- ATR
- ADX
- Price Compression
- Volatility Filter

Implementation may combine multiple methods.

---

# 8. Risk Management

Position Size

10% of available USDT balance

Maximum Open Position

1

Stop Loss

1.5%

Take Profit

2.5%

Configurable

- Daily Loss Limit
- Maximum Trades Per Day
- Cooldown After Loss
- Risk Percentage

The bot MUST reject invalid position sizes.

---

# 9. Order Validation

Before placing any order:

Validate

- Exchange connection
- Trading pair
- Available balance
- Minimum order size
- Tick size
- Precision
- API response

Never send invalid orders.

---

# 10. Market Data Validation

Reject data if:

- Missing candles
- Duplicate timestamps
- Invalid OHLCV
- Invalid prices
- Corrupted exchange response

---

# 11. Notifications

Telegram Required

Events

- Bot Started
- Bot Stopped
- BUY
- SELL
- Take Profit
- Stop Loss
- Errors
- Warnings
- Daily Summary

---

# 12. Logging

Every important action must be logged.

Required

- Trades
- Errors
- API Calls
- Execution Time
- Warnings
- Strategy Decisions

Logging Levels

- INFO
- WARNING
- ERROR
- DEBUG

---

# 13. Configuration

All settings MUST be configurable.

Examples

- Exchange
- Trading Pair
- Timeframe
- Position Size
- Stop Loss
- Take Profit
- Telegram
- API Keys
- Paper Mode
- Live Mode

No hardcoded values.

---

# 14. Resume State

If the application restarts:

The bot MUST

- Detect existing positions
- Restore previous state
- Continue TP monitoring
- Continue SL monitoring
- Prevent duplicate orders

---

# 15. Emergency Stop

Immediately stop opening new positions if:

- Exchange unavailable
- Internet unavailable
- Daily Loss Limit reached
- Invalid balance detected
- Consecutive API failures exceed threshold
- Critical exception occurs

---

# 16. Backtesting

Required

Historical Simulation

Performance Report

Metrics

- Profit
- Loss
- Win Rate
- Drawdown
- Profit Factor
- Sharpe Ratio
- Trade History

---

# 17. Statistics

Generate

- Daily Profit
- Weekly Profit
- Monthly Profit
- Average Win
- Average Loss
- Win Rate
- Drawdown
- Total Trades

---

# 18. Dashboard

Future Version

Web Dashboard

Features

- Live Status
- Price
- Indicators
- Current Position
- Open Orders
- Trade History
- Performance

---

# 19. AI Features

Future Version

Provide

- Market Summary
- Trade Explanation
- Signal Explanation
- Trade Journal Analysis
- Performance Analysis
- Strategy Suggestions

AI must never execute trades automatically without strategy approval.

---

# 20. Folder Structure

Fixed

bot/

core/

exchange/

strategy/

risk/

notification/

paper/

live/

backtest/

analytics/

config/

tests/

docs/

logs/

No unnecessary folders.

---

# 21. Code Quality

Required

- Python 3.x
- PEP8
- Type Hints
- Docstrings
- Meaningful Variable Names
- No Magic Numbers
- No Duplicate Code
- Clear Exception Handling

---

# 22. Testing

Every module must include testing where applicable.

Required

- Unit Test
- Integration Test

Every milestone must run successfully before merge.

---

# 23. Performance

The bot must

- Respect Exchange Rate Limits
- Minimize API Calls
- Cache reusable data
- Recover from temporary failures

---

# 24. Development Rules

Mandatory

- Every commit must run.
- Every milestone must work.
- Never commit broken code.
- Never skip milestones.
- Never rewrite unrelated modules.
- Never change architecture without approval.
- Never add dependencies without approval.

---

# 25. Git Rules

One Feature

↓

One Commit

↓

One Working State

Examples

GOOD

v0.0.4 implement EMA200 calculation

BAD

Implemented everything

---

# 26. Milestones

v0.0.1

Connect Exchange

Fetch BTC Price

v0.0.2

Fetch OHLCV

v0.0.3

EMA 200

v0.0.4

RSI 14

v0.0.5

ADX 14

v0.0.6

Sideways Detection

v0.0.7

Paper BUY

v0.0.8

Paper SELL

v0.0.9

Telegram Notification

v0.1

Complete Paper Trading

v0.2

Risk Management

v0.3

Statistics

v0.5

Backtesting

v1.0

Production Ready Spot Trading Bot

---

# 27. Definition of Done

A milestone is considered COMPLETE only if:

- Code runs successfully
- No critical errors
- Feature works as specified
- Logs generated correctly
- Tests pass (where applicable)
- Ready to commit

If these conditions are not met,

the milestone is NOT complete.

---

END OF SPECIFICATION

Status:

LOCKED

Any modification requires explicit approval from the project owner.
