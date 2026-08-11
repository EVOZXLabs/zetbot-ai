# Termux:Boot — auto-start on reboot

`zetbot-start.sh` is the hook that brings the bot + watchdog back up whenever
the Android device reboots (or Termux is killed and restarted).

Install (on your phone):

```bash
# 1. Termux:Boot app (F-Droid ONLY — the Play Store build is outdated)
#    https://f-droid.org/packages/com.termux.boot/

# 2. Open Termux:Boot once so it creates ~/.termux/boot/

# 3. Copy + enable the hook
mkdir -p ~/.termux/boot
cp scripts/termux-boot/zetbot-start.sh ~/.termux/boot/
chmod +x ~/.termux/boot/zetbot-start.sh

# 4. OPTIONAL: point to a non-default install path
echo 'export ZETBOT_ROOT=/data/data/com.termux/files/home/my-zetbot' >> ~/.bashrc
```

After every reboot the hook:

1. Waits `ZETBOT_BOOT_DELAY` (default 20s) for the device/network to settle.
2. Calls `scripts/termux-start.sh` (idempotent — no duplicate tmux sessions).
3. Logs everything to `$HOME/zetbot-boot.log`.

Troubleshooting:

- Bot didn't start after reboot → read `cat ~/zetbot-boot.log`.
- Termux:Boot itself was frozen → whitelist Termux:Boot **and** Termux from
  Android battery optimization (see "Menjalankan di Termux" in OPERATIONS.md).
