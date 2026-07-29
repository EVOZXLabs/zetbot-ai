"""
System diagnostics for ZetBot AI.

Runs comprehensive checks on all subsystems and displays
PASS/WARNING/FAIL results.
"""

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

from scripts.config_manager import env_exists, env_is_valid, config_to_dict


REQUIRED_DEPENDENCIES = ["ccxt", "requests", "dotenv", "colorama"]
REQUIRED_FOLDERS = ["data", "logs"]


class DiagnosticResult:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, category: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append({
            "category": category, "name": name,
            "status": status, "detail": detail,
        })

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
        print("\n" + "=" * 60)
        print("  ZetBot AI — System Diagnostics")
        print("=" * 60)
        current_category = ""
        for c in self.checks:
            if c["category"] != current_category:
                current_category = c["category"]
                print(f"\n  [{current_category}]")
            icon = {"PASS": "\u2705", "WARNING": "\u26a0\ufe0f", "FAIL": "\u274c"}.get(c["status"], "\u2753")
            detail = f" — {c['detail']}" if c["detail"] else ""
            print(f"    {icon} {c['name']}{detail}")
        print(f"\n  Summary: {self.passed} passed, {self.warnings} warnings, {self.failed} failed")


def _check_python(res: DiagnosticResult) -> None:
    py_ver = sys.version.split()[0]
    try:
        major, minor = map(int, py_ver.split(".")[:2])
        if major >= 3 and minor >= 10:
            res.add("Python", "Version", "PASS", py_ver)
        else:
            res.add("Python", "Version", "FAIL", f"{py_ver} (need 3.10+)")
    except Exception:
        res.add("Python", "Version", "WARNING", py_ver)


def _check_dependencies(res: DiagnosticResult) -> None:
    missing: list[str] = []
    broken: list[str] = []
    for mod in REQUIRED_DEPENDENCIES:
        try:
            __import__(mod.replace("-", "_"))
        except ImportError:
            missing.append(mod)
        except Exception:
            broken.append(mod)
    if missing:
        res.add("Dependencies", "Missing", "FAIL", ", ".join(missing) + " — run pip install")
    if broken:
        res.add("Dependencies", "Broken", "FAIL", ", ".join(broken))
    if not missing and not broken:
        res.add("Dependencies", "All", "PASS")


def _check_internet(res: DiagnosticResult) -> None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("google.com", 80))
        s.close()
        res.add("Internet", "Connectivity", "PASS")
    except socket.gaierror:
        res.add("Internet", "Connectivity", "FAIL", "DNS resolution failed")
    except OSError:
        res.add("Internet", "Connectivity", "FAIL", "No internet connection")


def _check_exchange(res: DiagnosticResult) -> None:
    if not env_exists():
        res.add("Exchange", "Config", "WARNING", "no .env file")
        return
    try:
        from scripts.app_config import load_config
        config = load_config()
        res.add("Exchange", "Configured", "PASS", config.exchange)

        from scripts.exchange_providers import get_provider_class
        import ccxt
        try:
            ccxt_name = get_provider_class(config.exchange)().ccxt_name
        except KeyError:
            res.add("Exchange", "Available", "FAIL", f"Unknown exchange: {config.exchange}")
            return
        exchange_class = getattr(ccxt, ccxt_name, None)
        if exchange_class is None:
            res.add("Exchange", "Available", "FAIL", f"CCXT has no exchange: {ccxt_name}")
            return
        res.add("Exchange", "Available", "PASS")
        exchange = exchange_class({"timeout": 5000})
        status = exchange.fetch_status() if hasattr(exchange, "fetch_status") else {}
        status_str = status.get("status", "ok") if status else "ok"
        res.add("Exchange", "API Status", "PASS", status_str)
        server_time = exchange.fetch_time() if hasattr(exchange, "fetch_time") else 0
        if server_time:
            delay = abs(int(time.time() * 1000) - server_time)
            res.add("Exchange", "Server Time", "PASS", f"{delay}ms delay")
    except Exception as exc:
        res.add("Exchange", "Connection", "FAIL", str(exc)[:80])


def _check_telegram(res: DiagnosticResult) -> None:
    if not env_exists():
        res.add("Telegram", "Config", "WARNING", "no .env file")
        return
    try:
        from scripts.app_config import load_config
        config = load_config()
        has_creds = bool(config.telegram_token and config.telegram_chat_id)
        if config.telegram_enabled and has_creds:
            res.add("Telegram", "Configuration", "PASS", "enabled with credentials")
        elif config.telegram_enabled:
            res.add("Telegram", "Configuration", "WARNING", "missing token or chat ID")
        else:
            res.add("Telegram", "Configuration", "WARNING", "disabled")
    except Exception as exc:
        res.add("Telegram", "Configuration", "FAIL", str(exc)[:80])


def _check_filesystem(res: DiagnosticResult) -> None:
    for folder in REQUIRED_FOLDERS:
        if os.path.isdir(folder):
            res.add("Filesystem", f"Folder: {folder}", "PASS")
        else:
            res.add("Filesystem", f"Folder: {folder}", "WARNING", "will be created")
    data_dir = "data"
    if os.path.isdir(data_dir):
        files = os.listdir(data_dir)
        json_files = [f for f in files if f.endswith(".json")]
        res.add("Filesystem", "Data files", "PASS", f"{len(json_files)} JSON files")


def _check_env_file(res: DiagnosticResult) -> None:
    if not env_exists():
        res.add("Config", ".env", "FAIL", "file not found")
        return
    if env_is_valid():
        res.add("Config", ".env", "PASS")
    else:
        res.add("Config", ".env", "FAIL", "invalid configuration")


def _check_logs(res: DiagnosticResult) -> None:
    if os.path.isdir("logs"):
        log_files = [f for f in os.listdir("logs") if f.endswith(".log")]
        if log_files:
            latest = max(log_files, key=lambda f: os.path.getmtime(f"logs/{f}"))
            size = os.path.getsize(f"logs/{latest}")
            res.add("Logs", "Available", "PASS", f"{latest} ({size} bytes)")
        else:
            res.add("Logs", "Available", "WARNING", "no log files yet")
    else:
        res.add("Logs", "Available", "WARNING", "logs directory missing")


def run_diagnostics() -> DiagnosticResult:
    res = DiagnosticResult()
    _check_python(res)
    _check_dependencies(res)
    _check_internet(res)
    _check_env_file(res)
    _check_exchange(res)
    _check_telegram(res)
    _check_filesystem(res)
    _check_logs(res)
    return res
