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

Never place multiple positions simultaneously.

Always validate balances.

Always validate exchange responses.

Never ignore exceptions.

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
