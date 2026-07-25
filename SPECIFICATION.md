# ZetBot AI Specification

Version: 3.0

Status: LOCKED

Owner: EVOZXLabs

Project: ZetBot AI

Document Type: Software Requirement Specification (SRS)

Last Updated: 2026

---

# 1. Mission

Develop a professional cryptocurrency Spot Trading Bot that is:

- Safe
- Stable
- Reliable
- Modular
- Maintainable
- Extensible
- Testable
- Production Ready

The bot must evolve gradually from market analysis into a fully automated Spot Trading System capable of operating continuously (24/7) after successfully passing paper trading, backtesting, and risk validation.

The primary objective of ZetBot AI is NOT maximum trading frequency.

The primary objective is long-term sustainable profitability while protecting trading capital.

---

# 2. Vision

ZetBot AI is designed to become a complete cryptocurrency trading operating system.

The system must:

- Monitor markets continuously.
- Analyze market conditions.
- Generate transparent trading signals.
- Execute disciplined risk management.
- Support paper trading before live trading.
- Learn from historical performance.
- Provide analytical recommendations.
- Operate autonomously with minimal user intervention.

The project is intended for long-term development and continuous improvement.

---

# 3. Core Philosophy

Every design decision inside ZetBot AI must follow these principles.

Priority order:

1. Protect Capital
2. Risk Management
3. Consistency
4. Stability
5. Compounding
6. Performance
7. Profit

Capital preservation is always more important than chasing higher returns.

Missing one profitable opportunity is acceptable.

Taking an unnecessary loss is not.

---

# 4. Design Philosophy

ZetBot AI should behave like a disciplined professional trader.

The bot should:

- Trade only when conditions are favorable.
- Avoid emotional decisions.
- Avoid unnecessary trades.
- Prefer waiting over forcing entries.
- Avoid ranging markets.
- Respect predefined risk limits.
- Execute strategies consistently.

When uncertainty exists,

the safest decision is:

DO NOTHING.

---

# 5. Project Goals

Primary goals:

- Build a production-ready Spot Trading Bot.
- Maintain a clean and modular architecture.
- Ensure every feature is testable.
- Make every trading decision explainable.
- Minimize technical debt.
- Preserve backward compatibility between milestones.

Secondary goals:

- Support future AI analysis.
- Support advanced backtesting.
- Support portfolio management.
- Support multiple exchanges.
- Support multiple trading strategies.

---

# 6. Scope

Included in Version 1.0

- Spot Trading
- Paper Trading
- Live Trading
- Market Analysis
- Technical Indicators
- Strategy Engine
- Risk Management
- Telegram Notification
- Statistics
- Trade Journal
- Backtesting
- Resume State
- Emergency Stop

Not included in Version 1.0

- Margin Trading
- Futures Trading
- Options Trading
- Copy Trading
- Arbitrage
- Grid Trading
- High Frequency Trading
- AI-controlled autonomous strategy modification

---

# 7. Supported Exchanges

Initial Exchanges

- Binance Spot
- Bybit Spot
- Tokocrypto Spot

Architecture Requirements

- Exchange-independent.
- Implemented through CCXT.
- Easy addition of future exchanges.
- No exchange-specific business logic inside strategy modules.

---

# 8. Trading Modes

Supported

- Paper Trading
- Live Trading

Future

- Backtesting
- Walk Forward Testing

Forbidden

- Margin
- Futures
- Leverage
- Options

Spot Trading ONLY.

---

# 9. Development Principles

The repository must evolve incrementally.

Every milestone must:

- Compile successfully.
- Execute successfully.
- Pass all existing tests.
- Preserve previous functionality.

Never:

- Commit broken code.
- Skip milestones.
- Leave unfinished implementations.
- Create placeholder functions.
- Rewrite architecture without approval.
- Introduce unnecessary complexity.

One Feature

↓

One Commit

↓

One Working Release

---

# 10. Coding Standards

Programming Language

Python 3.x

Style Guide

PEP8

Requirements

- Type Hints
- Docstrings
- Logging
- Meaningful Names
- Small Functions
- Clear Exception Handling
- Minimal Duplication
- Modular Design

Avoid

- Magic Numbers
- Hidden Side Effects
- Circular Imports
- Global Mutable State

---

# 11. Development Workflow

Every milestone follows the same workflow.

Step 1

Design

↓

Step 2

Implementation

↓

Step 3

Testing

↓

Step 4

Verification

↓

Step 5

Commit

↓

Step 6

Push

↓

Step 7

Review

A milestone is never considered complete before review.

---

# 12. Git Workflow

Rules

One Feature

↓

One Commit

↓

One Working State

Commit messages must be meaningful.

Examples

GOOD

v0.0.5 add ADX indicator

GOOD

v0.0.8 implement paper buy

BAD

fix stuff

BAD

update project

---

# 13. Repository Rules

The repository is the source of truth.

Every commit must satisfy:

- Build successfully.
- Execute successfully.
- Pass all tests.
- Keep repository clean.
- Preserve compatibility.

Working tree must always be clean before starting a new milestone.

---

# 14. Documentation Rules

Every important module must include:

- Docstrings
- Usage examples
- Type hints
- Logging
- Unit tests

Documentation must always evolve together with implementation.

Documentation must never lag behind the codebase.

---

# 15. Quality Assurance

Quality is more important than development speed.

Before every commit verify:

- Code compiles.
- Tests pass.
- No regression introduced.
- Logging works.
- Exceptions handled.
- Documentation updated.

Never trade quality for faster delivery.

---

# PART 2 — System Architecture

---

# 16. Overall Architecture

ZetBot AI follows a layered architecture.

```
                User Configuration
                        │
                        ▼
                 Configuration Engine
                        │
                        ▼
                  Exchange Engine
                        │
                        ▼
                 Market Data Engine
                        │
                        ▼
                 Indicator Engine
                        │
                        ▼
                Market State Engine
                        │
                        ▼
                 Strategy Engine
                        │
                        ▼
                   Risk Engine
                        │
                        ▼
             Money Management Engine
                        │
                        ▼
               Paper Trading Engine
                        │
                        ▼
                Live Trading Engine
                        │
                        ▼
               Notification Engine
                        │
                        ▼
                 Statistics Engine
                        │
                        ▼
                 Trade Journal Engine
                        │
                        ▼
                  AI Advisor Engine
```

Each layer has a single responsibility.

Business logic must never bypass the architecture.

---

# 17. Folder Structure

Repository Structure

```
zetbot-ai/

bot/

core/

exchange/

market/

indicator/

strategy/

risk/

money/

paper/

live/

notification/

analytics/

journal/

backtest/

config/

utils/

tests/

docs/

logs/

main.py

README.md

SPECIFICATION.md

AGENTS.md
```

No unnecessary folders.

No duplicated functionality.

---

# 18. Configuration System

All configurable values MUST be centralized.

Never hardcode values inside business logic.

Examples

Exchange

Trading Pair

Timeframe

Position Size

Risk

Take Profit

Stop Loss

Telegram

API Keys

Paper Mode

Live Mode

Compounding

Maximum Trades

Daily Loss Limit

---

# 19. Exchange Engine

Responsibilities

- Connect exchange
- Authentication
- Fetch balances
- Fetch market data
- Fetch symbols
- Fetch ticker
- Fetch OHLCV
- Validate exchange responses
- Handle exchange exceptions

Exchange Engine MUST NOT contain:

- Indicators
- Strategy
- Trading decisions

---

# 20. Market Data Engine

Responsibilities

- Download candles
- Validate candles
- Normalize column names
- Convert timestamps
- Handle missing candles
- Reject corrupted data
- Cache reusable market data

Market Data is read-only.

Never modify historical candles.

---

# 21. Indicator Engine

Responsibilities

Calculate indicators.

Current indicators

- EMA200
- RSI14
- ADX14
- ATR14

Future indicators

- MACD
- Bollinger Bands
- SuperTrend
- VWAP

Rules

Indicators never execute trades.

Indicators never communicate with exchanges.

Indicators only calculate values.

---

# 22. Market State Engine

Purpose

Classify current market condition.

Possible outputs

TRENDING

SIDEWAYS

Future

VOLATILE

LOW_VOLATILITY

Decision sources

ADX

ATR

Volatility Filter

Price Compression

The engine should prefer SIDEWAYS when uncertain.

---

# 23. Strategy Engine

Purpose

Generate trading signals.

Allowed outputs

BUY

SELL

HOLD

Strategy Engine never executes trades.

Strategy Engine never sends orders.

Strategy Engine only evaluates market conditions.

Every signal must include explanation.

Example

Signal

BUY

Reasons

- Price above EMA200
- RSI oversold
- ADX confirms trend
- Market is trending

---

# 24. Risk Engine

Purpose

Protect trading capital.

Responsibilities

Maximum Open Position

Daily Loss Limit

Cooldown

Maximum Trades

Stop Loss

Take Profit

Emergency Stop

Risk Engine has the highest execution priority.

If Risk Engine rejects a trade,

the trade MUST NOT execute.

---

# 25. Money Management Engine

Purpose

Determine position size.

Supported modes

Fixed Amount

Percentage Balance

Risk Percentage

Compounding

Default Mode

Risk Percentage

Risk Percentage is the default PRODUCTION mode.

Default Production Values

Risk Per Trade: 1%

Stop Loss: 1.5%

Take Profit: 3%

Position: Dynamic Calculation

Maximum Open Position: 1

Daily Loss Limit: 3%

Dynamic Position Sizing

Position size is NEVER a fixed dollar amount. It must always be
calculated dynamically from the current account balance:

Risk Amount = Account Balance × Risk Percentage

Position Size = Risk Amount / Stop Loss Distance

Because the formula is always evaluated against the CURRENT balance,
the bot automatically adjusts position size for any capital size,
for example:

$10

$100

$1,000

$10,000

Money Management never generates signals.

---

# 26. Paper Trading Engine

Purpose

Simulate trading.

Requirements

No real exchange orders.

No real balance changes.

Maintain virtual balance.

Maintain virtual positions.

Calculate

Entry

Exit

Profit

Loss

Commission

Trade Duration

Paper Trading must behave exactly like Live Trading.

Only execution differs.

---

# 27. Live Trading Engine

Purpose

Execute real Spot orders.

Responsibilities

Submit Buy

Submit Sell

Validate responses

Handle retries

Handle failures

Respect exchange limits

Never execute orders without Strategy approval.

Never bypass Risk Engine.

---

# 28. Position Manager

Responsibilities

Track active position.

Allowed states

NONE

OPEN

TAKE_PROFIT

STOP_LOSS

CLOSED

CANCELLED

Only one OPEN position is allowed in Version 1.0.

---

# 29. Resume State

After restart

Restore

Current Position

Entry Price

SL

TP

Virtual Balance

Trade History

Resume monitoring automatically.

Never duplicate positions.

---

# 30. Emergency Stop

Trigger when

Exchange unavailable

Internet unavailable

Critical Exception

Daily Loss Limit exceeded

API Failure threshold exceeded

Emergency Stop prevents opening NEW positions.

Existing positions continue monitoring whenever possible.

---

# 31. Notification Engine

Supported

Telegram

Future

Discord

Email

Webhook

Events

Bot Started

Bot Stopped

BUY

SELL

TP

SL

Errors

Warnings

Daily Summary

---

# 32. Statistics Engine

Calculate

Daily Profit

Weekly Profit

Monthly Profit

Average Profit

Average Loss

Win Rate

Profit Factor

Maximum Drawdown

Total Trades

Average Holding Time

Largest Win

Largest Loss

---

# 33. Trade Journal

Every completed trade must be recorded.

Fields

Date

Time

Exchange

Pair

Signal

Entry

Exit

Position Size

Stop Loss

Take Profit

Profit

Loss

Duration

Reasons

Market State

Indicators

The journal becomes the primary data source for AI analysis.

---

# 34. AI Advisor

Purpose

Analyze completed trades.

Provide recommendations.

Examples

Increase ADX threshold.

Reduce risk.

Avoid specific trading hours.

Disable weak trading pairs.

Restrictions

AI MUST NEVER

Modify strategy automatically.

Modify live parameters automatically.

Execute trades automatically.

AI is an advisor.

Human approval is always required.

---

# 35. Multi-Pair Architecture

Version 1.0

Primary Pair

BTC/USDT

Future

ETH/USDT

BNB/USDT

SOL/USDT

Architecture must support multiple pairs without redesign.

---

# 36. Scalability

Every engine must be independent.

Future additions should require minimal modification.

Examples

New Indicator

New Strategy

New Exchange

New Notification

New Dashboard

No engine should depend directly on unrelated modules.

---

# PART 3 — Trading System

---

# 37. Trading Philosophy

The objective of ZetBot AI is NOT to trade as often as possible.

The objective is to execute only high-quality trades.

The preferred decision order is:

DO NOTHING

↓

WAIT

↓

BUY

↓

SELL

The bot should always prefer waiting instead of forcing trades.

No trade is better than a bad trade.

---

# 38. Trading Timeframe

Default

1 Hour (1H)

Supported

15m

30m

1H

4H

Future

Daily Confirmation

Weekly Confirmation

The default production timeframe is 1H.

---

# 39. Strategy

Primary Strategy

Trend Following with RSI Pullback

BUY Conditions

Every condition MUST be TRUE.

✓ Price > EMA200

✓ RSI14 < 30

✓ ADX >= Configurable Threshold

✓ Market State == TRENDING

✓ Volume Filter Passed

✓ No Active Position

✓ Cooldown Inactive

✓ Daily Loss Limit NOT reached

✓ Maximum Daily Trades NOT reached

SELL Conditions

SELL whenever ANY becomes TRUE.

Take Profit

Stop Loss

Exit Strategy

Emergency Exit

Strategy Engine only generates signals.

Execution is handled elsewhere.

---

# 40. Signal States

Allowed signals

BUY

SELL

HOLD

No additional signals are allowed.

Every signal MUST include reasons.

Example

Signal

BUY

Reasons

- EMA200 Bullish

- RSI Oversold

- ADX Strong

- Market Trending

---

# 41. Trend Detection

Bullish

Price > EMA200

Bearish

Price < EMA200

Only bullish trend allows BUY.

Bearish trend blocks BUY.

---

# 42. RSI Rules

Default

14 Period

BUY

RSI < 30

Future

Adaptive RSI

Dynamic Threshold

Machine Learning Recommendation

---

# 43. ADX Rules

Default

14 Period

Purpose

Measure trend strength.

Suggested Default Threshold

25

Configurable

20–35

ADX does NOT determine direction.

ADX only determines trend strength.

---

# 44. ATR Rules

Purpose

Measure market volatility.

ATR is used by

Sideways Detection

Future Dynamic Stop Loss

Future Trailing Stop

ATR never generates BUY signals.

---

# 45. Sideways Detection

Market State

TRENDING

SIDEWAYS

Decision combines

ADX

ATR

Volatility

Price Compression

If confidence is low

Return

SIDEWAYS

Never force TRENDING.

---

# 46. Position Rules

Maximum Open Position

1

Duplicate BUY signals

Ignored

SELL without position

Ignored

Every position has

Entry

SL

TP

Quantity

Timestamp

Status

---

# 47. Money Management

Supported Modes

Fixed Amount

Percentage Balance

Risk Percentage

Compounding

Default Mode

Risk Percentage (default PRODUCTION mode)

Default Production Values

Risk Per Trade: 1%

Stop Loss: 1.5%

Take Profit: 3%

Position: Dynamic Calculation

Maximum Open Position: 1

Daily Loss Limit: 3%

Position size in Risk Percentage mode is always computed as:

Risk Amount = Account Balance × Risk Percentage

Position Size = Risk Amount / Stop Loss Distance

This means position size automatically adapts to the account balance,
whether the balance is $10, $100, $1,000, or $10,000.

No hardcoded values.

Everything configurable.

---

# 48. Compounding

Default

Enabled

Mode

Percentage Balance

Example

Balance

100

Trade

10

↓

Balance

150

Trade

15

↓

Balance

500

Trade

50

Compounding must respect Risk Engine.

---

# 49. Risk Rules

Default

Position Size

Dynamic Calculation (Risk Amount / Stop Loss Distance)

Maximum Daily Loss

3%

Maximum Daily Trades

Configurable

Maximum Open Position

1

Risk always overrides strategy.

---

# 50. Stop Loss

Default

1.5%

Supported

1%

1.5%

2%

Custom

Future

ATR Dynamic Stop

Trailing Stop

---

# 51. Take Profit

Default

3%

Supported

2.5%

3%

5%

Custom

Future

Partial Take Profit

Trailing Take Profit

---

# 52. Cooldown

Cooldown activates after

Stop Loss

Purpose

Prevent revenge trading.

Configurable.

Default

1 Candle

---

# 53. Daily Protection

If Daily Loss Limit reached

↓

Stop opening new positions.

Continue monitoring existing position.

Resume next trading day.

---

# 54. Profit Protection

Future Feature

Optional

If Daily Profit Target reached

↓

Stop opening new trades.

Resume next trading day.

Purpose

Prevent overtrading.

---

# 55. Paper Trading

Paper Trading MUST simulate:

Entry

Exit

Commission

Profit

Loss

Duration

Balance

Paper Trading should behave identically to Live Trading.

Only execution differs.

---

# 56. Live Trading

Live Trading is disabled until:

Paper Trading validated.

Backtesting completed.

Risk validation passed.

Owner approval received.

No live order may execute before these conditions.

---

# 57. Trade Journal

Every completed trade must store

Timestamp

Exchange

Pair

Entry

Exit

Quantity

Position Size

SL

TP

Commission

Profit

Loss

Duration

Signal

Reasons

Indicators

Market State

Trade Journal is permanent.

Never silently delete history.

---

# 58. Trading Statistics

Generate

Daily Profit

Weekly Profit

Monthly Profit

Win Rate

Average Win

Average Loss

Profit Factor

Sharpe Ratio

Maximum Drawdown

Longest Win Streak

Longest Loss Streak

Average Holding Time

---

# 59. Trading Schedule

Default

24 Hours

7 Days

Future

Trading Sessions

Time Filters

News Filters

Holiday Filters

---

# 60. Strategy Profiles

Supported

Conservative

Balanced

Aggressive

Each profile changes only configuration.

Never architecture.

---

# 61. Future Strategy Expansion

Future strategies

EMA Cross

Breakout

MACD

Bollinger

SuperTrend

AI Assisted

Every strategy must implement the same interface.

No duplicated business logic.

---

# 62. Trading Safety

The bot MUST always prefer

NO TRADE

instead of

LOW QUALITY TRADE.

Capital preservation is always the highest priority.

---

# PART 4 — Analytics, AI & Intelligence

---

# 63. Statistics Engine

The Statistics Engine is responsible for calculating trading performance.

Required Metrics

- Daily Profit
- Weekly Profit
- Monthly Profit
- Total Profit
- Win Rate
- Loss Rate
- Profit Factor
- Average Win
- Average Loss
- Maximum Drawdown
- Largest Win
- Largest Loss
- Longest Win Streak
- Longest Loss Streak
- Average Holding Time
- Total Trading Days
- Total Trades

Statistics must always be reproducible.

---

# 64. Performance Analytics

The bot must continuously evaluate trading performance.

Examples

Performance by

- Exchange
- Trading Pair
- Timeframe
- Strategy
- Market Condition

Performance reports must identify strengths and weaknesses.

---

# 65. Trade Journal

Every completed trade MUST be permanently stored.

Required fields

Trade ID

Date

Time

Exchange

Trading Pair

Timeframe

Signal

Entry Price

Exit Price

Quantity

Position Size

Stop Loss

Take Profit

Commission

Gross Profit

Net Profit

Duration

Exit Reason

Market State

Indicator Values

Strategy Version

Risk Profile

Bot Version

The journal is the primary historical dataset.

---

# 66. AI Advisor

Purpose

Analyze historical trading performance.

Provide intelligent recommendations.

Examples

- Increase ADX threshold.
- Reduce daily trade limit.
- Lower position size.
- Avoid weak trading pairs.
- Avoid specific trading hours.
- Improve Stop Loss.
- Improve Take Profit.

The AI Advisor NEVER executes trades.

The AI Advisor NEVER changes configuration automatically.

Human approval is always required.

---

# 67. Learning Engine

Purpose

Identify patterns from historical trades.

Analyze

Winning Trades

Losing Trades

Market Conditions

Holding Time

Indicators

Volatility

Trading Sessions

The engine generates recommendations only.

It never modifies trading behavior automatically.

---

# 68. Market Intelligence

Future Feature

Generate market summaries.

Examples

Current Trend

Market Volatility

Momentum

Support and Resistance

Risk Level

The objective is to improve user understanding.

---

# 69. Trade Explanation

Every generated signal should be explainable.

Example

Signal

BUY

Reasons

- Price above EMA200
- RSI oversold
- ADX confirms strong trend
- Market is TRENDING

The explanation must be human-readable.

---

# 70. Performance Recommendation

Future AI reports may recommend

Lower Risk

Increase Risk

Disable Pair

Increase ADX Threshold

Reduce RSI Threshold

Change Trading Session

Every recommendation requires owner approval.

---

# 71. Pair Analysis

Future

Compare performance by pair.

Examples

BTC/USDT

ETH/USDT

SOL/USDT

BNB/USDT

Generate

Win Rate

Profit

Loss

Drawdown

Average Trade

---

# 72. Session Analysis

Analyze trading performance by time.

Examples

00:00–06:00 UTC

06:00–12:00 UTC

12:00–18:00 UTC

18:00–24:00 UTC

Determine

Best Trading Session

Worst Trading Session

Future recommendations may suggest disabling weak sessions.

---

# 73. Strategy Analysis

Future

Support multiple strategies.

Compare

Win Rate

Drawdown

Profit Factor

Average Trade

The system must identify the strongest strategy.

---

# 74. Risk Analysis

Analyze

Stop Loss frequency

Take Profit frequency

Average Risk

Average Reward

Maximum Consecutive Loss

Maximum Consecutive Win

Risk recommendations must be generated automatically.

---

# 75. Dashboard

Future Version

Dashboard displays

Bot Status

Exchange

Trading Pair

Current Price

Market State

Indicators

Open Position

Daily Profit

Win Rate

Statistics

Trade History

System Health

---

# 76. System Health

Monitor

CPU Usage

Memory Usage

Internet Connection

Exchange Status

Telegram Status

Bot Uptime

Restart Count

Future

Automatic health reporting.

---

# 77. Daily Report

Automatically generate a daily report.

Example

Date

Trades

Wins

Losses

Profit

Loss

Current Balance

Best Pair

Worst Pair

Maximum Drawdown

Recommendations

---

# 78. Weekly Report

Generate

Weekly Profit

Win Rate

Trade Count

Drawdown

Performance Comparison

AI Recommendations

---

# 79. Monthly Report

Generate

Monthly Return

Monthly Drawdown

Monthly Win Rate

Best Strategy

Worst Strategy

Recommended Improvements

---

# 80. AI Restrictions

The AI system MUST NEVER

Execute trades.

Modify live configuration.

Modify strategy automatically.

Override Risk Engine.

Ignore Stop Loss.

Ignore Emergency Stop.

AI is an analytical assistant.

The owner always has final authority.

---

# 81. Continuous Improvement

The purpose of AI is continuous improvement.

Workflow

Collect Data

↓

Analyze

↓

Generate Insights

↓

Owner Reviews

↓

Owner Approves

↓

Configuration Updated

↓

Repeat

This keeps ZetBot adaptive while maintaining full human control.

---

# PART 5 — Development, Deployment & Production

---

# 82. Testing Policy

Testing is mandatory.

Every new feature MUST include tests.

Required

- Unit Test
- Integration Test

Future

- Performance Test
- Stress Test

Every milestone must pass all previous tests.

Regression is NOT allowed.

---

# 83. Test Coverage

Minimum goals

Critical modules

100%

Overall project

>90%

Critical modules include

- Strategy Engine
- Risk Engine
- Paper Trading
- Live Trading
- Position Manager

---

# 84. Continuous Verification

Every milestone must pass

Code Quality

↓

Static Analysis

↓

Unit Test

↓

Integration Test

↓

Manual Verification

↓

Commit

↓

Push

No milestone may skip verification.

---

# 85. Logging Policy

Every important action must be logged.

Required

- Bot Started
- Bot Stopped
- Exchange Connected
- Exchange Disconnected
- BUY
- SELL
- TP
- SL
- Errors
- Exceptions
- Warnings
- Configuration Loaded
- Resume State
- Emergency Stop

Log Levels

DEBUG

INFO

WARNING

ERROR

CRITICAL

---

# 86. Error Handling

Every exception must be handled.

Never silently ignore exceptions.

Every unexpected exception must

- Log Error
- Notify Telegram
- Continue safely when possible

Critical failures trigger Emergency Stop.

---

# 87. Configuration Management

Configuration must be centralized.

Never hardcode values.

Supported configuration

Exchange

Trading Pair

Timeframe

Risk

Position Size

Compounding

Paper Mode

Live Mode

Telegram

API Keys

Logging

Statistics

AI

Configuration changes should not require source code modification.

---

# 88. Secrets Management

Sensitive information MUST NEVER be committed.

Examples

API Keys

Telegram Tokens

Passwords

Private Keys

Secrets must be loaded from configuration or environment variables.

.gitignore must exclude sensitive files.

---

# 89. Deployment

Primary Target

Ubuntu Server

Recommended

Ubuntu 24.04 LTS

Python

3.12

Future

Docker

Cloud Deployment

Deployment must be reproducible.

---

# 90. VPS Requirements

Minimum

1 vCPU

1 GB RAM

20 GB SSD

Ubuntu 24.04 LTS

Recommended

2 vCPU

2 GB RAM

40 GB SSD

The bot should run efficiently on low-cost VPS.

---

# 91. Production Runtime

The production bot is expected to operate

24 Hours

7 Days

Requirements

Automatic Restart

Resume State

Stable Memory Usage

Graceful Shutdown

Automatic Recovery

---

# 92. Restart Policy

Unexpected shutdown

↓

Restart

↓

Restore Position

↓

Restore Balance

↓

Resume Monitoring

↓

Continue Trading

The bot must never duplicate positions after restart.

---

# 93. Internet Failure

If internet connection is lost

Pause new trades.

Continue monitoring existing position whenever possible.

Reconnect automatically.

Resume operation safely.

---

# 94. Exchange Failure

If exchange API becomes unavailable

Retry

↓

Reconnect

↓

Validate State

↓

Resume

If recovery fails

Trigger Emergency Stop.

---

# 95. Telegram Failure

Telegram failure MUST NEVER stop trading.

Notification failures should

Retry

↓

Log

↓

Continue

Trading remains unaffected.

---

# 96. Resource Usage

The bot should minimize

CPU

Memory

Disk Usage

API Calls

Network Traffic

Efficient resource usage has priority over unnecessary calculations.

---

# 97. Performance Rules

The bot must

Respect exchange rate limits.

Cache reusable market data.

Avoid duplicate API requests.

Avoid unnecessary calculations.

Optimize indicator calculations.

---

# 98. Git Workflow

Workflow

Implement

↓

Run Tests

↓

Verify

↓

Commit

↓

Push

↓

Review

↓

Next Milestone

Never develop multiple milestones simultaneously.

---

# 99. Branch Policy

Primary Branch

main

Future

develop

feature/*

bugfix/*

Version 1.0 may continue using a single stable branch.

---

# 100. Versioning

Semantic Versioning

v0.x.x

Development

v1.0.0

Production Release

Future

v1.x.x

Bug Fixes

v2.x.x

Major Features

---

# 101. Release Policy

Every release must include

Release Notes

Version Number

Git Tag

Passing Tests

Updated Documentation

Clean Repository

---

# 102. Production Checklist

Before enabling Live Trading

Paper Trading validated

Backtesting completed

Risk Engine verified

Statistics verified

Telegram verified

Resume State verified

Emergency Stop verified

Manual approval completed

If any requirement fails

Live Trading remains disabled.

---

# 103. Maintenance

The project should remain

Modular

Maintainable

Documented

Testable

Extensible

Readable

Technical debt should be minimized.

---

# 104. Future Compatibility

The architecture should support

Additional Exchanges

Additional Strategies

Additional Indicators

Multiple Trading Pairs

Dashboard

Database

Machine Learning

Cloud Deployment

Without major redesign.

---

# 105. Long-Term Objective

ZetBot AI is designed to evolve from

Trading Bot

↓

Professional Trading Platform

↓

Trading Intelligence System

↓

AI Assisted Trading Operating System

The architecture must always support future expansion while preserving stability.

---

# PART 6 — Project Constitution

---

# 106. Development Roadmap

The project evolves incrementally.

No milestone may be skipped.

Every milestone must be fully completed before the next milestone begins.

Roadmap

v0.0.1
Connect Exchange

v0.0.2
Fetch OHLCV

v0.0.3
EMA200

v0.0.4
RSI14

v0.0.5
ADX14

v0.0.6
ATR + Market State

v0.0.7
Strategy Engine

v0.0.8

Paper Trading Engine

Objectives

- Open Paper Position
- Close Paper Position
- Virtual Balance
- Position Manager
- Trade Journal
- PnL Calculation

v0.0.9

Telegram Notification

Objectives

- BUY Notification
- SELL Notification
- TP Notification
- SL Notification
- Error Notification
- Daily Summary

v0.1.0

Complete Paper Trading

Objectives

- Full Trading Loop
- Position Lifecycle
- Statistics
- Validation

v0.2.0

Risk Engine

Objectives

- Daily Loss Protection
- Maximum Trades
- Cooldown
- Risk Profiles

v0.3.0

Statistics & Journal

Objectives

- Performance Reports
- Trade Journal
- Analytics

v0.4.0

AI Advisor

Objectives

- Trade Analysis
- Strategy Suggestions
- Performance Review

v0.5.0

Backtesting

Objectives

- Historical Testing
- Reports
- Strategy Comparison

v0.6.0

Live Trading

Objectives

- Spot Execution
- Live Balance
- Resume State

v0.7.0

Multi Pair

Objectives

BTC

ETH

BNB

SOL

v0.8.0

Performance Optimization

Objectives

Memory

CPU

API Calls

Recovery

v0.9.0

Production Candidate

Objectives

Stress Test

Bug Fix

Final Review

v1.0.0

Production Release

---

# 107. Definition of Done

A milestone is COMPLETE only if ALL conditions are TRUE.

✓ Feature implemented.

✓ Documentation updated.

✓ Tests added.

✓ Previous tests pass.

✓ Code reviewed.

✓ Repository clean.

✓ Commit created.

✓ Push successful.

✓ No known critical bugs.

Otherwise

The milestone is NOT complete.

---

# 108. Regression Policy

New code must never break existing functionality.

Regression is considered a critical bug.

Regression must be fixed immediately.

---

# 109. Technical Debt Policy

Avoid technical debt whenever possible.

If technical debt is introduced,

it must be documented.

Never leave undocumented technical debt.

---

# 110. Refactoring Policy

Refactoring is allowed only when

- Improves maintainability
- Improves readability
- Improves performance
- Does not change behaviour

Architecture refactoring requires explicit owner approval.

---

# 111. Dependency Policy

Every dependency must have a clear purpose.

Avoid unnecessary packages.

Prefer built-in Python libraries whenever practical.

Heavy libraries require justification.

---

# 112. Security Policy

Never expose

API Keys

Private Keys

Passwords

Secrets

Sensitive information must never be committed into Git.

Security always has priority over convenience.

---

# 113. Reliability Policy

The bot should always prefer

Safe behaviour

over

Fast behaviour.

Examples

Better to skip one trade

than execute a dangerous trade.

Better to stop trading

than continue after a critical failure.

---

# 114. Trading Principles

Always remember

Capital Protection

↓

Risk Management

↓

Consistency

↓

Compounding

↓

Profit

Never reverse this order.

---

# 115. Engineering Principles

Every module should have

One responsibility.

Every function should

Do one thing well.

Readable code is preferred over clever code.

Simple solutions are preferred over complex solutions.

---

# 116. AI Principles

Artificial Intelligence exists to

Assist

Explain

Analyze

Recommend

AI does NOT exist to

Override Strategy

Ignore Risk

Execute Unauthorized Trades

Modify Live Configuration

Human approval is always required.

---

# 117. Owner Principles

The owner has final authority over

Configuration

Risk

Deployment

Strategy

Live Trading

The bot is a tool.

The owner is always responsible for final decisions.

---

# 118. Long-Term Vision

ZetBot AI should evolve through four stages.

Stage 1

Professional Spot Trading Bot

↓

Stage 2

Professional Trading Platform

↓

Stage 3

Trading Intelligence System

↓

Stage 4

AI Assisted Trading Operating System

Every future feature should support this vision.

---

# 119. Project Values

The project values

Quality

Safety

Transparency

Maintainability

Reliability

Scalability

Consistency

Documentation

Testing

Long-Term Thinking

These values should guide every engineering decision.

---

# 120. Final Constitution

This specification is the Constitution of ZetBot AI.

Every developer,

AI coding agent,

automation,

or contributor

MUST follow this document.

If implementation and specification conflict,

the specification takes precedence.

Any modification to this document requires explicit approval from the Project Owner.

No exceptions.

---

END OF SPECIFICATION

Version

3.0

Status

LOCKED

Owner

EVOZXLabs

Project

ZetBot AI

Motto

"Protect Capital. Trade with Discipline. Grow with Consistency."
