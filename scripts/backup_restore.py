"""
Backup and Restore for ZetBot AI.

Creates timestamped ZIP archives of configuration, data, and logs.
Validates backup integrity before restoring.

BackupScheduler:
    Hourly background thread that calls create_backup() and prunes
    archives older than 7 days (configurable via BACKUP_RETENTION_DAYS).
"""

import datetime
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from typing import Optional

logger = logging.getLogger("ZetBot")

BACKUP_DIR = "backups"
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS", "3600"))  # 1 hour

INCLUDE_PATHS = [
    ".env",
    "data/paper_balance.json",
    "data/paper_orders.json",
    "data/positions.json",
    "data/state.json",
    "data/scanner_results.json",
    "data/decision_results.json",
    "data/risk_results.json",
    "data/trade_plan.json",
    "data/paper_state.json",
]
INCLUDE_DIRS = ["logs", "data"]


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure_backup_dir() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)


def create_backup() -> str:
    """Create a timestamped backup ZIP. Returns the backup file path."""
    _ensure_backup_dir()
    ts = _timestamp()
    backup_path = os.path.join(BACKUP_DIR, f"backup-{ts}.zip")

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        written: set[str] = set()

        for path in INCLUDE_PATHS:
            if os.path.exists(path) and path not in written:
                zf.write(path, path)
                written.add(path)

        for directory in INCLUDE_DIRS:
            if os.path.isdir(directory):
                for root, _dirs, files in os.walk(directory):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        if fpath not in written:
                            zf.write(fpath, fpath)
                            written.add(fpath)

        from scripts.config_import_export import VERSION
        info = {
            "created_at": ts,
            "version": VERSION,
            "includes": {
                "files": [p for p in INCLUDE_PATHS if os.path.exists(p)],
                "dirs": [d for d in INCLUDE_DIRS if os.path.isdir(d)],
            },
        }
        zf.writestr("backup-info.json", json.dumps(info, indent=2))

    return backup_path


def validate_backup(backup_path: str) -> bool:
    """Validate a backup ZIP file. Returns True if valid."""
    if not os.path.exists(backup_path):
        return False
    if not zipfile.is_zipfile(backup_path):
        return False
    try:
        with zipfile.ZipFile(backup_path, "r") as zf:
            names = zf.namelist()
            if "backup-info.json" not in names:
                return False
            bad = zf.testzip()
            if bad is not None:
                return False
        return True
    except Exception:
        return False


def list_backups() -> list[dict[str, str]]:
    """List available backups sorted by date (newest first)."""
    _ensure_backup_dir()
    backups: list[dict[str, str]] = []
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fname.endswith(".zip"):
            fpath = os.path.join(BACKUP_DIR, fname)
            size = os.path.getsize(fpath)
            valid = validate_backup(fpath)
            backups.append({
                "filename": fname,
                "path": fpath,
                "size": f"{size:,} bytes",
                "valid": "\u2705" if valid else "\u274c",
            })
    return backups


def restore_backup(backup_path: str, confirm: bool = True) -> bool:
    """Restore a backup ZIP. Validates first. Returns True on success."""
    if not validate_backup(backup_path):
        print(f"Invalid backup: {backup_path}")
        return False

    if confirm:
        ans = input(f"Restore from {backup_path}? Existing files will be overwritten [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Restore cancelled.")
            return False

    try:
        with zipfile.ZipFile(backup_path, "r") as zf:
            for name in zf.namelist():
                if name == "backup-info.json":
                    continue
                dirname = os.path.dirname(name)
                if dirname:
                    os.makedirs(dirname, exist_ok=True)
                zf.extract(name)
        print(f"Restored from {backup_path}")
        return True
    except Exception as exc:
        print(f"Restore failed: {exc}")
        return False


def prune_old_backups(
    backup_dir: str = BACKUP_DIR,
    retention_days: int = BACKUP_RETENTION_DAYS,
) -> list[str]:
    """Delete backup ZIPs older than ``retention_days`` days.

    Returns a list of file paths that were deleted.
    """
    if not os.path.isdir(backup_dir):
        return []
    cutoff = time.time() - retention_days * 86400
    removed: list[str] = []
    for fname in os.listdir(backup_dir):
        if not fname.endswith(".zip"):
            continue
        fpath = os.path.join(backup_dir, fname)
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed.append(fpath)
                logger.info("Backup pruned (older than %d days): %s", retention_days, fpath)
        except OSError as exc:
            logger.warning("Could not prune backup %s: %s", fpath, exc)
    return removed


class BackupScheduler:
    """Background thread that creates hourly backups and prunes old ones.

    Usage::

        scheduler = BackupScheduler()
        scheduler.start()          # call once at bot startup
        ...
        scheduler.stop()           # call at shutdown

    The first backup is taken immediately on ``start()``.  Subsequent
    backups run every ``interval_seconds`` (default 3600 = 1 hour).
    Archives older than ``retention_days`` (default 7) are pruned after
    each backup cycle.

    A backup is also written immediately when ``backup_now()`` is called,
    e.g. before a restart or deploy.
    """

    def __init__(
        self,
        backup_dir: str = BACKUP_DIR,
        interval_seconds: int = BACKUP_INTERVAL_SECONDS,
        retention_days: int = BACKUP_RETENTION_DAYS,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self._backup_dir = backup_dir
        self._interval = interval_seconds
        self._retention = retention_days
        self._shutdown = shutdown_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="BackupScheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "BackupScheduler started: interval=%ds retention=%dd dir=%s",
            self._interval, self._retention, self._backup_dir,
        )

    def stop(self) -> None:
        self._running = False
        self._shutdown.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def backup_now(self) -> str:
        """Create an immediate backup and prune old archives. Returns path."""
        try:
            path = create_backup()
            logger.info("Backup created: %s", path)
        except Exception as exc:
            logger.warning("Backup failed: %s", exc)
            return ""
        try:
            pruned = prune_old_backups(self._backup_dir, self._retention)
            if pruned:
                logger.info("Pruned %d old backup(s)", len(pruned))
        except Exception as exc:
            logger.warning("Backup pruning failed: %s", exc)
        return path

    def _run(self) -> None:
        # Take an immediate backup on startup, then sleep between cycles.
        self.backup_now()
        while self._running:
            # Sleep in short increments so shutdown is responsive.
            elapsed = 0.0
            while elapsed < self._interval and self._running:
                self._shutdown.wait(timeout=min(30.0, self._interval - elapsed))
                if self._shutdown.is_set():
                    self._running = False
                    return
                elapsed += 30.0
            if self._running:
                self.backup_now()
