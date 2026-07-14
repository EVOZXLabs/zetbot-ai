"""
Backup and Restore for ZetBot AI.

Creates timestamped ZIP archives of configuration, data, and logs.
Validates backup integrity before restoring.
"""

import datetime
import json
import os
import shutil
import tempfile
import zipfile
from typing import Optional

BACKUP_DIR = "backups"
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
