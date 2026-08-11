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
- [ ] Killed repeatedly: after more than `WATCHDOG_MAX_RESTARTS` (default `3`)
      crashes inside `WATCHDOG_WINDOW` (default `600`) seconds, the watchdog
      HALTS and notifies "MANUAL INTERVENTION NEEDED" — see **Halt paths**
      below for the exact crash-rate message.

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

## Scenario 6 — Heartbeat Stale (Suspend Blip vs. Genuinely Hung Bot)

**Goal:** Verify the watchdog does **not** kill a bot that merely resumed
from device suspend (its heartbeat refreshes during the one-interval grace
re-check), but does kill+restart a genuinely hung bot and finally **HALTS**
after `WATCHDOG_MAX_HEARTBEAT_STALE_RESTARTS` (default `3`) consecutive
stale observations instead of looping forever.

The bot writes `data/watchdog_heartbeat.json` every ~60s. A stale heartbeat
means "alive by PID but unresponsive". The watchdog never kills on the
first stale detection alone — it always re-checks after one check interval
(default `20` s). Heartbeat-stale restarts are **not** crashes and never
feed the crash-rate limit; they have their own streak-based halt.

> Faster test: shorten the threshold so the checklist runs quickly:
> `WATCHDOG_HEARTBEAT_STALE=60 python scripts/watchdog.py` (default `300`).

### 6a — Device suspend / wake blip (must NOT restart)

Simulate Android Doze / laptop suspend: freeze the bot for longer than the
stale threshold, then resume it. A stopped process is still alive by PID but
cannot write heartbeats.

```bash
kill -STOP $(cat data/zetbot.pid)   # freeze (e.g. > WATCHDOG_HEARTBEAT_STALE s)
kill -CONT $(cat data/zetbot.pid)   # wake the bot
```

Observe (first stale detection + grace re-check + refresh):

```
[WARNING] bot heartbeat stale for 350s (threshold 300s) — re-checking after one interval before restarting
[INFO]    heartbeat refreshed (age 30s) — bot resumed (e.g. device wake); no restart
```

Pass criteria:

- [ ] Bot is **not** killed or restarted
- [ ] No "BOT HEARTBEAT STALE" / "BOT RESTARTED (auto)" Telegram notification
- [ ] Watchdog returns to `running` and the heartbeat streak is reset

### 6b — Genuinely hung bot (kill/restart, then streak HALT)

Add a temporary deadlock to `main.py` so the heartbeat is never written
(every respawn hits it too — this is what makes the streak reach the halt):

```python
# At the top of the keep-alive loop, BEFORE write_heartbeat():
import threading
threading.Event().wait()  # block forever — alive but never heartbeats
```

Run with watchdog (default or shortened `WATCHDOG_HEARTBEAT_STALE`):

```bash
python scripts/watchdog.py
```

Observe (first detection → re-check → streak kill/restart → halt):

```
[WARNING] bot heartbeat stale for 305s (threshold 300s) — re-checking after one interval before restarting
[WARNING] bot heartbeat STILL stale after re-check (age 315s) — restarting
[INFO]    spawning bot: .venv/bin/python main.py
[WARNING] bot heartbeat STILL stale after re-check (age 320s) — restarting     # streak 2
[INFO]    spawning bot: .venv/bin/python main.py
[ERROR]   halting auto-restart: The bot was restarted 2 times but kept failing to write a heartbeat (still alive by PID but unresponsive).
```

Pass criteria:

- [ ] No kill happens on the **first** stale detection — the re-check is observed first
- [ ] Bot is killed and restarted **at most** `WATCHDOG_MAX_HEARTBEAT_STALE_RESTARTS - 1` (default 2) times, then halted on the final stale observation
- [ ] `data/.watchdog_halt` is written; the halt is **sticky** across watchdog restarts until you `rm data/.watchdog_halt`
- [ ] Telegram notification: "MANUAL INTERVENTION NEEDED" with the **heartbeat-streak reason** (not the crash-rate reason)
- [ ] No infinite kill/restart loop

---

## Halt paths (the two ways the watchdog stops auto-restart)

Both write the sticky `data/.watchdog_halt` and notify "MANUAL INTERVENTION
NEEDED", but the reason text and log line differ — this is what an operator
running the checklist will actually see.

### 1. Crash-rate halt (Scenario 4)

Trigger: more than `WATCHDOG_MAX_RESTARTS` (default `3`) **real crashes**
inside `WATCHDOG_WINDOW` (default `600`) seconds. Heartbeat-stale restarts
never count toward this.

Log:

```
[ERROR] rate limit exceeded (4 crashes in 600s) — halting auto-restart
```

Notification:

```
🚨 *WATCHDOG* — MANUAL INTERVENTION NEEDED

The bot has restarted more than 3 times in the last 10 min (4 crashes) and keeps dying.
Auto-restart has been HALTED to avoid a crash loop.

Fix the bug, then restart the watchdog:
  `.venv/bin/python scripts/watchdog.py`

⚠️ While the bot is down there is NO SL/TP protection on exchanges without native stop orders (e.g. indodax).
```

### 2. Heartbeat-streak halt (Scenario 6b)

Trigger: `WATCHDOG_MAX_HEARTBEAT_STALE_RESTARTS` (default `3`) consecutive
observations where the heartbeat is STILL stale after the one-interval grace
re-check. Independent of the crash rate limit — a suspend/wake blip that
refreshes the heartbeat within the grace never reaches it.

Log:

```
[ERROR] halting auto-restart: The bot was restarted 2 times but kept failing to write a heartbeat (still alive by PID but unresponsive).
```

Notification:

```
🚨 *WATCHDOG* — MANUAL INTERVENTION NEEDED

The bot was restarted 2 times but kept failing to write a heartbeat (still alive by PID but unresponsive).
Auto-restart has been HALTED to avoid a restart loop.

Fix the issue, then restart the watchdog:
  `.venv/bin/python scripts/watchdog.py`

⚠️ While the bot is down there is NO SL/TP protection on exchanges without native stop orders (e.g. indodax).
```

---

## Automated Regression Tests

The scenarios above are partially automated in:

```
tests/test_exit_crash_recovery.py   # TP/SL crash recovery
tests/test_ghost_position_sync.py   # ghost position detection
tests/test_paper_state_lock_concurrency.py  # concurrent write safety
tests/test_batch1_hardening.py      # balance/lock/atomic write
tests/test_batch2_hardening.py      # watchdog heartbeat/pipeline staleness
tests/test_audit_repros.py          # watchdog stale-heartbeat grace/streak; notified_buys; daemon startup
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
- [ ] Scenario 6 passed (heartbeat stale: suspend/wake blip NOT restarted; hung bot halted after streak)
- [ ] All automated tests passing: `pytest` → 0 failures
- [ ] `.env` API key has **no Withdrawal permission**
- [ ] `PAPER_MODE=false` only after all above are confirmed
