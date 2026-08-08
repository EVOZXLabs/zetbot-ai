# Smoke-Test Audit — Bug Report & Fixes

Date: 2026-08-08

Audit triggered by the 2026-08-07 smoke test:

| Metric | Smoke test reported |
|---|---|
| Cash | 997,663.60 IDR |
| Open trades | 49,883.18 IDR |
| Total Balance | 1,047,546.78 IDR |
| Closed P&L | +8,177.22 IDR |
| Initial | 1,000,000 IDR |

Expected Total Balance = Initial + Closed P&L = 1,008,177.22 IDR. The
reported numbers did not reconcile, and no Telegram BUY_OPENED arrived
despite the log showing `APPROVED THRESHOLD/IDR R:R 3.64 49,883.18 IDR`.

---

## BUG-1 — SafeGuard counted the planned symbol as already open (blocked its own BUY)

### BUG FOUND
The pipeline runs stages in order:
`Scanner → Decision → Risk → Trade → Position → Paper`.

The Position stage (`position_manager.py`) **simulates** every READY plan
into `data/positions.json` with status `OPEN` — *before* the Paper stage
executes anything. The Paper stage then asked
`SafeGuard.can_open_new_position()`, which counts open positions from
`positions.json` and hit `Max open positions reached: 1/1` — counting the
simulated THRESHOLD/IDR entry as the blocking position. The real BUY was
skipped, the position remained a ghost (OPEN in positions.json, absent
from `paper_state.json`), and no BUY_OPENED was ever sent.

### CAUSE
`SafeGuard._check_max_open_positions()` (scripts/safety_limits.py:144)
reads `positions.json` as its only source of truth and has no knowledge
of which symbol's simulation was written this cycle for execution.

### FIX
- `SafeGuard.set_planned_symbols()` records the symbols the pipeline is
  about to open.
- `SafeGuard._check_max_open_positions()` excludes those symbols from the
  open-position count (still counts every genuinely open *other* symbol).
- `Pipeline._run_paper_di()` reads the READY plans FIRST, sets the planned
  symbols on the safeguard, then runs the guard.

### TEST
`tests/test_smoke_audit_fixes.py::TestSafeGuardPlannedSymbols` —
planned symbol does not block its own BUY; a real other position still
blocks; stale planned symbols are cleared between cycles.

---

## BUG-2 — Monitor double-credited a closure the pipeline already handled

### BUG FOUND
At 2026-08-06 00:39:10 PRIME/IDR hit its stop loss. The pipeline's
reconciliation sold 7.92211 (paper_state credited 33,609.38), then 5 ms
later the position monitor executed `_update_paper_on_closure` with the
FULL original quantity 11.3173 and credited the wallet a second time
(+48,013.41) plus appended a duplicate SELL order to
`paper_orders.json` (the `SELL PRIME/IDR qty 11.3173 net_pnl -2066.46`
row). Both threads read positions.json as OPEN and both triggered the SL.

### CAUSE
`main._update_paper_on_closure` had no idempotency guard: it always
re-sold the quantity and always credited `paper_balance.json` /
`paper_state.json`, even when the provider's `execute_sell` had already
closed and credited the same position.

### FIX
`_update_paper_on_closure` now reads `paper_state.json` (the
authoritative ledger) first:
- position CLOSED there → the pipeline already credited → skip the
  wallet credit, the trade counters, the realized PnL, and the SELL order
  append (positions.json bookkeeping + notification still happen);
- position absent → simulated ghost, never bought → also no credit;
- ledger file missing entirely → legacy monitor-only behavior preserved.

### TEST
`tests/test_smoke_audit_fixes.py::TestMonitorClosureIdempotent` —
an already-closed position is not credited twice, no duplicate order,
trade counters unchanged; an OPEN ledger position still credits exactly
once.

---

## BUG-3 — paper_balance.json `final_equity` excluded open position value

### BUG FOUND
`/wallet` reported Open trades 49,883.18 IDR but
`paper_balance.json` `final_equity` was frozen at `final_balance`
(997,663.60) — the reported "Total Balance 1,047,546.78" came from the
Telegram command (cash + position value) while the persisted file said
equity == cash. Any report or restart reading the file understated
equity by the full open-position value.

### CAUSE
`Pipeline._persist_paper_state` wrote `final_equity = final_balance`,
ignoring open positions — contradicting the canonical invariant
`equity == cash + position_market_value`.

### FIX
`_persist_paper_state` computes equity through
`MetricsManager.compute_snapshot(cash, open_positions=...)` using the
provider's OPEN positions (`current_price × remaining_qty`), so the
persisted `final_equity` always satisfies the invariant.

### TEST
`tests/test_smoke_audit_fixes.py::TestEquityIncludesOpenPositions` —
with an open position, persisted equity == cash + position value; with
no positions, equity == cash.

---

## BUG-4 — Ghost positions produced notifications and polluted accounting

### BUG FOUND
`data/positions.json` contained `BTC/USDT OPEN` (order_id "o1", a test
fixture leftover) and the earlier THRESHOLD/IDR ghost — entries with NO
counterpart in `paper_state.json` and NO BUY order. They were treated as
real: restart recovery sent a BUY_OPENED notification for BTC/USDT, the
monitor could close them (crediting phantom proceeds), and they inflated
exposure/equity.

### CAUSE
`main._notify_existing_positions` notified every OPEN position in
`positions.json` with no check against the ledger; nothing pruned
simulated-but-never-executed entries.

### FIX
- `_notify_existing_positions` only sends BUY_OPENED for symbols present
  as OPEN in `paper_state.json` (logs a debug skip for ghosts).
- `Pipeline._prune_ghost_positions` (after the Paper stage, PAPER mode
  only) marks any remaining positions.json OPEN entry without an OPEN
  ledger counterpart as CLOSED so ghosts stop inflating state.

### TEST
`tests/test_smoke_audit_fixes.py::TestGhostPositions` — ghost never
notified, ledger-backed position still notified, pipeline prunes ghosts.

---

## TASK 4 — CCXT root-cause logging

### BUG FOUND
Exchange retry failures logged the call label and exception text but not
the exchange name or the exception CLASS, making root-cause analysis
(temporary network blip vs exchange outage vs auth) harder than needed.

### FIX
`exchange_call_with_retry` gained an `exchange` context parameter and
every failure is logged as
`Exchange call '<exchange>: <method(symbol)>' failed (attempt n/N):
<CCXTExceptionClass> — <detail>`.
All `BaseProvider` call sites (get_ticker, fetch_ohlcv, fetch_balance,
fetch_tickers, load_markets) pass `exchange=self.name`.

### TEST
`tests/test_smoke_audit_fixes.py::TestExchangeRootCauseLogging` —
log records contain exchange name, method/symbol label, and exception
class for NetworkError and RequestTimeout.

---

## Verification

- New regression tests: 13 (tests/test_smoke_audit_fixes.py).
- Existing tests updated to the corrected contracts:
  - tests/test_production_fixes.py (restart recovery seeds the ledger)
  - tests/test_audit_repros.py (failed-delivery retry seeds the ledger)
  - tests/test_indodax_live_execution.py (fake safeguard gains
    `set_planned_symbols`)
- Full suite: **1431 passed, 45 skipped, 0 failed**.
- Real-data sanity check (2026-08-08): THRESHOLD/IDR is now a genuine
  ledger fill (paper_state OPEN, order FILLED), `.notified_buys` contains
  THRESHOLD/IDR, and the BTC/USDT ghost is slated for pruning on the next
  pipeline cycle.
