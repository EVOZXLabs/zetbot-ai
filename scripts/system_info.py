"""
System Information for ZetBot AI.

Displays version, git, OS, CPU, memory, disk, and configuration.
"""

import datetime
import os
import platform
import shutil
import subprocess
import sys
from typing import Any

from scripts.config_import_export import VERSION


def get_system_info() -> str:
    """Return a formatted system information string."""
    lines = ["=== ZetBot AI — System Information ===\n"]

    lines.append(f"  Version:    {VERSION}")

    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        lines.append(f"  Git:        {r.stdout.strip() if r.returncode == 0 else 'N/A'}")
    except Exception:
        lines.append(f"  Git:        N/A")

    lines.append(f"  Python:     {sys.version.split()[0]}")
    lines.append(f"  Platform:   {sys.platform}")
    lines.append(f"  OS:         {platform.system()} {platform.release()}")

    try:
        cpu_info = platform.processor()
        if not cpu_info and os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        cpu_info = line.split(":")[1].strip()
                        break
        lines.append(f"  CPU:        {cpu_info or 'N/A'}")
    except Exception:
        lines.append(f"  CPU:        N/A")

    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
        lines.append(f"  Memory:     {mem:.1f} GB total")
    except (ValueError, AttributeError):
        lines.append(f"  Memory:     N/A")

    try:
        usage = shutil.disk_usage(".")
        total_gb = usage.total / (1024**3)
        free_gb = usage.free / (1024**3)
        lines.append(f"  Disk:       {total_gb:.1f} GB total, {free_gb:.1f} GB free")
    except Exception:
        lines.append(f"  Disk:       N/A")

    try:
        from scripts.app_config import load_config
        config = load_config()
        lines.append(f"  Mode:       {'PAPER' if config.paper_mode else 'LIVE'}")
        lines.append(f"  Exchange:   {config.exchange}")
        lines.append(f"  Data Dir:   {os.path.abspath(config.data_dir)}")
        lines.append(f"  Scheduler:  {'enabled' if config.auto_pipeline else 'disabled'}")
    except Exception as exc:
        lines.append(f"  Config:     {exc}")

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"\n  Time:       {now}")

    return "\n".join(lines)
