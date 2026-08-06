# Crash Recovery & Power-Loss Test Scenarios

ZetBot AI Production Test Guide — verifying state integrity after forced
process termination or connectivity loss.

---

## Overview

ZetBot AI stores all trading state in `data/*.json` files using atomic
writes (temp-file → `os.replace`). This means:

- A crash **between** two writes can leave one file updated and another
  stale, but never a partially-written (corrupt) file.
- On restart, the pipeline re-reads all files from disk and reconciles
  any open positions against the current exchange state.

The scenarios below must be tested before going live with real capital.

---

## Scenario 1 — Process Kill Mid-Trade (Paper Mode)

**Goal:** Verify no double-order on restart after a BUY fills but before
positions.json is updated.

### Setup

```bash
PAPER_MODE=true python main.py &
```

Let the scanner find a candidate and approve a BUY. Watch for:

```
[INFO] ORDER_FILLED BTC/USDT ...
```

### Kill immediately after fill

```bash
kill -9 $(cat data/zetbot.pid)
```

### Verify state before restart

```bash
# positions.json should have the OPEN position
cat data/positions.json | python -m json.tool | grep -A5 '"status"'

# paper_balance.json should reflect the deducted balance
cat data/paper_balance.json | python -m json.tool | grep final_balance
```

### Restart

```bash
python main.py
```

### Pass criteria

- [ ] No duplicate BUY order for the same symbol
- [ ] Telegram notification says "STATE RESTORED" with the open position
- [ ] Balance in `/balance` matches the pre-crash balance minus cost
- [ ] `/positions` shows the position is still OPEN
- [ ] No `ORDER_REJECTED: already has open position` error in logs

---

## Scenario 2 — Process Kill During TP Sell (Paper Mode)

**Goal:** Verify TP1 is not re-sold after restart when kill happens after
the sell but before positions.json is updated.

### Setup

Open a paper position. Manually set `tp1` to a value just above current
price in `data/positions.json`, then wait for the pipeline to execute TP1.

### Kill immediately after TP1 SELL order is filled

```bash
# Watch logs for TP_TRIGGERED, then:
kill -9 $(cat data/zetbot.pid)
```

### Verify

```bash
# tp1_hit must be true (write-ahead log saved it)
cat data/positions.json | python -m json.tool | grep tp1_hit
```

### Restart

```bash
python main.py
```

### Pass criteria

- [ ] `tp1_hit: true` survives in positions.json
- [ ] No second TP1 sell on restart
- [ ] Remaining quantity = original quantity × 0.70 (70% left after TP1)
- [ ] No negative remaining_qty

---

## Scenario 3 — Internet Disconnection Mid-Cycle

**Goal:** Verify the bot pauses trading (exchange cooldown) when the
exchange is unreachable, and resumes automatically when connectivity
returns.

### Setup

```bash
python main.py &
```

### Simulate disconnection

```bash
# Block outbound connections to exchange
sudo iptables -A OUTPUT -d api.binance.com -j DROP
# (or for Indodax: indodax.com)
```

### Observe

After 3 consecutive exchange failures within 5 minutes, the bot writes:
```
data/exchange_cooldown.json
```
and logs:
```
[WARNING] Exchange cooldown activated for 600s
```

### Restore connectivity

```bash
sudo iptables -D OUTPUT -d api.binance.com -j DROP
```

### Pass criteria

- [ ] No orders submitted while exchange is unreachable
- [ ] `data/exchange_cooldown.json` exists and `active: true` while down
- [ ] After cooldown expires, `/pipeline` runs successfully
- [ ] No crash — bot stays running throughout
- [ ] Open positions are NOT closed during disconnection

---

## Scenario 4 — Watchdog Restart After Crash

**Goal:** Verify the watchdog detects a crash and restarts within ~20s.

### Setup

```bash
# Terminal 1: start watchdog (it spawns the bot)
python scripts/watchdog.py

# Terminal 2: watch watchdog log
tail -f logs/watchdog.log
```

### Kill the bot (simulate crash)

```bash
kill -9 $(cat data/zetbot.pid)
```

### Observe

Within 20 seconds (watchdog interval):
```
[WARNING] bot down (exit code -9) — restarting
[INFO]    spawning bot: .venv/bin/python main.py
```

### Pass criteria

- [ ] Bot restarts within 20 seconds
- [ ] Telegram notification: "BOT RESTARTED (auto)"
- [ ] After restart, `/status` is responsive
- [ ] If killed 4× in 10 minutes, watchdog halts and notifies "MANUAL INTERVENTION NEEDED"

---

## Scenario 5 — Power Loss / Reboot (Full System)

**Goal:** Verify state survives a full system restart.

### Setup

Have an active paper position open. Note down:
```bash
cat data/positions.json | python -m json.tool
cat data/paper_balance.json | python -m json.tool | grep final_balance
```

### Simulate power loss

```bash
sudo reboot
# or: sudo shutdown -h now (then power back on)
```

### Restart bot

```bash
cd /path/to/zetbot-ai
python main.py
```

### Pass criteria

- [ ] `data/*.json` files are intact (no truncation from partial writes)
- [ ] Bot detects existing open position and resumes monitoring it
- [ ] `/balance` shows correct balance (not reset to initial)
- [ ] If systemd watchdog service is active: bot auto-starts on boot
- [ ] SL/TP monitoring resumes for the open position

---

## Scenario 6 — Heartbeat Stale (Bot Hung)

**Goal:** Verify the watchdog kills and restarts a bot that is alive but
unresponsive (e.g. deadlocked).

### Setup

Add a temporary deadlock to `main.py` for testing:

```python
# In the keep-alive loop, after ~60s:
import threading
threading.Event().wait()  # block forever
```

Run with watchdog:

```bash
python scripts/watchdog.py
```

### Observe (after WATCHDOG_HEARTBEAT_STALE seconds, default 300s)

```
[WARNING] bot heartbeat stale for 305s — process alive but unresponsive; restarting
```

### Pass criteria

- [ ] Watchdog kills the hung process
- [ ] Bot restarts cleanly
- [ ] `data/watchdog_heartbeat.json` is refreshed after restart
- [ ] Telegram notification: "BOT HEARTBEAT STALE"

---

## Automated Regression Tests

The scenarios above are partially automated in:

```
tests/test_exit_crash_recovery.py   # TP/SL crash recovery
tests/test_ghost_position_sync.py   # ghost position detection
tests/test_paper_state_lock_concurrency.py  # concurrent write safety
tests/test_batch1_hardening.py      # balance/lock/atomic write
```

Run all:

```bash
pytest tests/ -v
```

---

## Checklist Before Live Trading

- [ ] Scenario 1 passed (kill mid-BUY, no duplicate)
- [ ] Scenario 2 passed (kill mid-TP, no re-sell)
- [ ] Scenario 3 passed (internet down, no blind orders)
- [ ] Scenario 4 passed (watchdog restarts in ≤20s)
- [ ] Scenario 5 passed (power loss, state intact)
- [ ] Scenario 6 passed (heartbeat stale → watchdog kills and restarts)
- [ ] All automated tests passing: `pytest` → 0 failures
- [ ] `.env` API key has **no Withdrawal permission**
- [ ] `PAPER_MODE=false` only after all above are confirmed
