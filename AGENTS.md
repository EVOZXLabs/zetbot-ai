# AGENTS.md

# ZetBot AI Development Rules

This repository is developed by AI agents.

Every AI agent MUST follow these rules.

---

# Primary Goal

Build a professional cryptocurrency Spot Trading Bot.

The project is production-oriented.

Do not create demo code.

Do not create placeholder implementations.

---

# Modular Command Architecture (Telegram)

Commands live in `telegram/commands/` — one file per command.  
To add a new command:

1. Create `telegram/commands/your_command.py`
2. Define a class inheriting from `BaseCommand` with `meta` + `execute()`
3. Done — auto-discovered by `CommandRegistry`

Do NOT:
- Edit `scripts/telegram_commands.py` to add commands
- Add `if/elif` blocks
- Manually register anything

---

# Source of Truth

The project specification is defined in:

SPECIFICATION.md

Always follow SPECIFICATION.md.

Never contradict it.

---

# Development Philosophy

Small working milestones.

Every commit must compile.

Every commit must run.

Never leave broken code.

Never create unfinished modules.

---

# Forbidden

Do NOT:

- invent architecture
- refactor without request
- rewrite unrelated files
- create unnecessary folders
- add dependencies without approval
- generate fake implementations

---

# Milestone Rules

Implement ONLY the current milestone.

Never jump ahead.

Never implement future features.

---

# Code Style

Python 3.x

PEP8

Type hints preferred

Meaningful variable names

Clear comments

Graceful exception handling

Logging where appropriate

---

# Commit Rules

One feature

One commit

One working state

Example

GOOD

v0.0.3 add EMA200 calculation

BAD

implement everything

---

# Trading Rules

Spot only.

Never margin.

Never futures.

Never leverage.

---

# Safety

Never place multiple positions for the same symbol simultaneously.

Always validate balances.

Always validate exchange responses.

Never ignore exceptions.

---

# Risk / Position Sizing Formula

Position sizing in ``scripts/risk_manager.py``:

1. ``MAX_RISK_PER_TRADE_PCT`` = 2.0 % of ``balance`` → risk_amount
2. ``position_size`` = risk_amount / stop_distance (entry - stop)
3. ``position_value`` = position_size × entry_price
4. ``max_position_value`` = available_capital × ``MAX_POSITION_SIZE_PCT`` (0.6 = 60 %)
5. Final position_value = min(step 3, step 4)

``MAX_POSITION_SIZE_PCT`` is a **portfolio-wide** exposure cap, not a per-position allowance. ``RiskManager`` tracks total committed capital — positions already open from previous pipeline cycles (``_existing_exposure``) plus positions approved earlier in the current run (``_used_capital``) — and only allows a new position up to whatever headroom remains under ``max_position_size_pct * equity``. Two 60 % positions can therefore never combine into 84-100 % exposure; the second position is capped to whatever % of equity remains under the 60 % ceiling (e.g. equity $10,000, first position $4,000 (40 %) -> second position capped at $2,000 so total stays at $6,000 / 60 %).

``main()`` (used by both the CLI entrypoint and the pipeline) resolves live ``balance``/``equity`` from ``data/paper_balance.json`` (``_resolve_account_state()``) before constructing ``RiskManager`` — it must never fall back to a hardcoded starting-balance constant once trading has begun, or the cap will be computed against stale capital and silently let exposure drift toward 100 %.

---

# Notification Pipeline (BUY_OPENED)

Every new BUY fill MUST send a BUY_OPENED notification:

1. ``scripts/paper_trading_engine.py`` ``_execute_plan()`` calls ``_notify_buy()`` immediately after the position is created (line 650).
2. ``_notify_buy()`` calls ``notifier.notify_buy_opened()``.
3. The notifier is threaded through: ``main.py`` → ``container.inject_notifier()`` → ``ServiceContainer`` → ``Pipeline`` → ``paper_trading_engine.main(notifier=…)``.
4. Notification failures never break trading (caught by ``_notify_buy`` try/except).
5. Restart recovery sends notifications via ``_notify_existing_positions()``, deduplicated via ``data/.notified_buys``.

---

# Equity / Accounting Rules

Equity calculation invariants:

- equity = cash (``final_balance``) + sum of market values of all OPEN positions
- market_value of a position = ``current_price × remaining_qty`` (or = ``cost_basis + unrealized_pnl``)
- position_value = equity - cash (derived, not read from positions.json)
- exposure_pct = (position_value / equity) × 100
- total_return_pct = ((equity - initial_balance) / initial_balance) × 100
- net_pnl = realized_pnl + unrealized_pnl

``scripts/accounting_reconcile.py`` ``correct_equity = cash + position_value + unrealized_pnl``
``main.py`` ``_update_paper_on_closure`` ``final_equity = final_balance + remaining_position_market_value``
``scripts/metrics_manager.py`` ``MetricsManager.account()`` reads equity from ``paper_balance.json`` — the single source of truth.

---

# AI Behaviour

Before changing code

Read SPECIFICATION.md

Read existing code

Explain planned changes

Implement

Run

Verify

Commit

---

# Priority

1. Correctness

2. Stability

3. Safety

4. Readability

5. Performance

---

# Final Rule

If uncertain,

ASK.

Do not guess.
