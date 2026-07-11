"""
Startup validation for ZetBot AI.

Verifies that the environment is ready before the bot starts.
"""

import os
import shutil
import subprocess
import sys
from typing import Any

from scripts.config_manager import env_exists, env_is_valid

REQUIRED_FOLDERS = ["data", "logs"]
REQUIRED_FILES: list[str] = []
REQUIRED_DEPENDENCIES = ["ccxt", "requests", "dotenv", "colorama"]


class ValidationResult:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "PASS")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "WARNING")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "FAIL")

    def print_report(self) -> None:
        print("\n=== Startup Validation ===\n")
        for c in self.checks:
            icon = {"PASS": "\u2705", "WARNING": "\u26a0\ufe0f", "FAIL": "\u274c"}.get(c["status"], "\u2753")
            detail = f" — {c['detail']}" if c["detail"] else ""
            print(f"  {icon} {c['name']}{detail}")
        print(f"\n  {self.passed} passed, {self.warnings} warnings, {self.failed} failed")
        if self.failed > 0:
            print("  Some checks failed. Fix issues before starting the bot.")


def _check_python(result: ValidationResult) -> None:
    py_ver = sys.version.split()[0]
    try:
        major, minor = map(int, py_ver.split(".")[:2])
        if major >= 3 and minor >= 10:
            result.add("Python", "PASS", py_ver)
        else:
            result.add("Python", "FAIL", f"{py_ver} (need 3.10+)")
    except Exception:
        result.add("Python", "WARNING", py_ver)


def _check_dependencies(result: ValidationResult) -> None:
    missing: list[str] = []
    for mod in REQUIRED_DEPENDENCIES:
        try:
            __import__(mod.replace("-", "_"))
        except ImportError:
            missing.append(mod)
    if not missing:
        result.add("Dependencies", "PASS")
    else:
        result.add("Dependencies", "FAIL", f"missing: {', '.join(missing)}")


def _check_folders(result: ValidationResult) -> None:
    missing: list[str] = []
    for folder in REQUIRED_FOLDERS:
        if not os.path.isdir(folder):
            missing.append(folder)
    if not missing:
        result.add("Required Folders", "PASS")
    else:
        result.add("Required Folders", "WARNING", f"will create: {', '.join(missing)}")
        for folder in missing:
            os.makedirs(folder, exist_ok=True)


def _check_data_files(result: ValidationResult) -> None:
    missing: list[str] = []
    for fname in REQUIRED_FILES:
        if not os.path.exists(fname):
            missing.append(fname)
    if not missing:
        result.add("Data Files", "PASS")
    else:
        result.add("Data Files", "WARNING", f"will be created on first run")


def _check_config(result: ValidationResult) -> None:
    if not env_exists():
        result.add("Configuration", "FAIL", ".env not found")
        return
    if not env_is_valid():
        result.add("Configuration", "FAIL", ".env contains invalid values")
        return
    result.add("Configuration", "PASS")


def _check_exchange_config(result: ValidationResult) -> None:
    try:
        from scripts.app_config import load_config
        config = load_config()
        result.add("Exchange", "PASS", config.exchange)
    except Exception as exc:
        result.add("Exchange", "WARNING", str(exc))


def _check_telegram_config(result: ValidationResult) -> None:
    try:
        from scripts.app_config import load_config
        config = load_config()
        has_creds = bool(config.telegram_token and config.telegram_chat_id)
        if config.telegram_enabled and has_creds:
            result.add("Telegram", "PASS", "enabled")
        elif config.telegram_enabled:
            result.add("Telegram", "WARNING", "enabled but missing token/chat ID")
        else:
            result.add("Telegram", "WARNING", "disabled")
    except Exception as exc:
        result.add("Telegram", "WARNING", str(exc))


def validate_all() -> ValidationResult:
    result = ValidationResult()
    _check_python(result)
    _check_dependencies(result)
    _check_config(result)
    _check_folders(result)
    _check_data_files(result)
    _check_exchange_config(result)
    _check_telegram_config(result)
    return result


def validate_startup() -> bool:
    """Run startup validation, print report, return True if ok to start."""
    result = validate_all()
    result.print_report()
    return result.failed == 0
