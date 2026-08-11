#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
#  ZetBot AI — Termux:Boot hook
#
#  Runs `termux-start.sh` automatically after the device boots so the bot and
#  its watchdog come back up without manual interaction.
#
#  INSTALLATION (one-time, on your phone):
#
#    1. Install the Termux:Boot app from F-Droid:
#         https://f-droid.org/packages/com.termux.boot/
#       (NOT the Play Store build — it is outdated.)
#
#    2. Open Termux:Boot once (it creates ~/.termux/boot/).
#
#    3. Copy + enable this hook:
#         mkdir -p ~/.termux/boot
#         cp scripts/termux-boot/zetbot-start.sh ~/.termux/boot/
#         chmod +x ~/.termux/boot/zetbot-start.sh
#
#    4. Termux:Boot needs to be exempt from battery optimization (see
#       "Menjalankan di Termux" in OPERATIONS.md), otherwise Android may
#       freeze it — and the watchdog with it.
#
#  HOW IT WORKS
#    * Locates the project: $ZETBOT_ROOT, else $HOME/zetbot-ai.
#    * Waits for the device to finish booting / network to come up.
#    * Calls scripts/termux-start.sh (idempotent — safe to run every boot).
#    * Logs to $HOME/zetbot-boot.log so you can inspect what happened.
#
#  NOTE: scripts in ~/.termux/boot run with no terminal — all output goes to
#  the log file, so check it if the bot did not start.
# =============================================================================

set -u

LOG="$HOME/zetbot-boot.log"
BOOT_DELAY="${ZETBOT_BOOT_DELAY:-20}"          # seconds to let the device settle
NETWORK_RETRIES="${ZETBOT_NETWORK_RETRIES:-5}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# --- 1. Locate the project ----------------------------------------------
if [ -n "${ZETBOT_ROOT:-}" ]; then
    ROOT="$ZETBOT_ROOT"
elif [ -f "$HOME/zetbot-ai/scripts/termux-start.sh" ]; then
    ROOT="$HOME/zetbot-ai"
else
    log "ERROR: project not found — set ZETBOT_ROOT or install to \$HOME/zetbot-ai"
    exit 1
fi
START="$ROOT/scripts/termux-start.sh"

if [ ! -x "$START" ]; then
    log "ERROR: $START not found/executable — is the project installed?"
    exit 1
fi

# --- 2. Give the device a moment (network, /data unlock, etc.) -----------
log "boot hook started (project: $ROOT), waiting ${BOOT_DELAY}s for the device to settle"
sleep "$BOOT_DELAY"

# --- 3. Start (retry a few times in case network is still coming up) -----
n=0
while [ "$n" -lt "$NETWORK_RETRIES" ]; do
    log "calling termux-start.sh (attempt $((n + 1))/$NETWORK_RETRIES)"
    if "$START" >> "$LOG" 2>&1; then
        log "OK: bot + watchdog started"
        exit 0
    fi
    n=$((n + 1))
    if [ "$n" -lt "$NETWORK_RETRIES" ]; then
        log "start failed (attempt $n) — retrying in 30s"
        sleep 30
    fi
done

log "ERROR: bot + watchdog could not be started after ${NETWORK_RETRIES} attempts"
log "       check: $LOG, tmux (pkg install tmux), Termux:API, and $ROOT/.env"
exit 1
