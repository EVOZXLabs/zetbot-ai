# ZetBot AI Specification

Version: 1.0

Owner: EVOZXLabs

Status: Locked

---

# Mission

Develop a professional cryptocurrency Spot Trading Bot that is safe, modular, maintainable, and production-ready.

The project must evolve incrementally.

Every commit must be executable.

No unfinished modules.

No placeholder implementations.

---

# Supported Exchanges

- Binance Spot
- Bybit Spot
- Tokocrypto Spot

Architecture must allow adding new exchanges through CCXT.

---

# Trading Strategy

Trend Following with RSI Filter

BUY only when:

- Price > EMA 200
- RSI(14) < 30
- Market is NOT Sideways
- No active position

SELL when:

- Take Profit reached
- Stop Loss reached
- Exit rule triggered

---

# Indicators

Required

EMA 200

RSI 14

ADX 14

ATR

Volume Filter

Future

MACD

Bollinger Bands

Multi Timeframe Confirmation

---

# Sideways Detection

Bot must avoid trading during ranging markets.

Possible methods

ATR

ADX

Price Compression

Volatility Filter

---

# Risk Management

Position Size

10% USDT Balance

Maximum Open Position

1

Stop Loss

1.5%

Take Profit

2.5%

Daily Loss Limit

Configurable

Maximum Trades Per Day

Configurable

Cooldown after Loss

Supported

---

# Notifications

Telegram

Bot Started

Bot Stopped

BUY

SELL

Take Profit

Stop Loss

Errors

Warnings

Daily Summary

---

# Logging

Every important event must be logged.

Trades

Errors

API Calls

Warnings

Execution Time

---

# Paper Trading

Must support paper trading before live trading.

---

# Live Trading

Spot Only

Never Margin

Never Futures

---

# Backtesting

Historical simulation

Performance report

Profit

Loss

Drawdown

Win Rate

Sharpe Ratio

---

# Statistics

Daily Profit

Weekly Profit

Monthly Profit

Win Rate

Average Profit

Average Loss

Maximum Drawdown

Trade History

---

# Dashboard

Future Version

Web Dashboard

Live Status

Charts

Open Position

Trade History

---

# AI Features

Future Version

Trade Analysis

Market Summary

Signal Explanation

Trade Journal Analysis

---

# Development Rules

Every commit must run.

Never leave broken code.

Never create unused modules.

Never refactor architecture before Version 1.0.

Build small working milestones.

Commit after every completed milestone.

---

# Milestones

v0.0.1

Connect Exchange

Fetch BTC Price

v0.0.2

Fetch OHLCV

v0.0.3

EMA 200

v0.0.4

RSI

v0.0.5

ADX

v0.0.6

Sideways Filter

v0.0.7

Paper Buy

v0.0.8

Paper Sell

v0.0.9

Telegram

v0.1

Complete Paper Trading

v0.5

Backtesting

v1.0

Production Spot Trading
