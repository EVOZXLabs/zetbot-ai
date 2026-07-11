"""
Interactive First-Setup Wizard for ZetBot AI.

Walks the user through configuration step by step,
generates .env, and validates the result.
"""

import os
from typing import Optional

from scripts.config_manager import (
    CONFIG_FIELDS,
    ENV_PATH,
    env_exists,
    env_is_valid,
    write_env,
    validate_env_dict,
    display_config,
    config_to_dict,
    reset_env,
)


CLEAR_SCREEN = "\033[2J\033[H"


def _input(prompt: str, default: str = "", secret: bool = False) -> str:
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    try:
        if secret:
            value = _getpass(prompt)
        else:
            value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        raise
    return value or default


def _getpass(prompt: str) -> str:
    """Simple password input without echo."""
    try:
        import getpass
        return getpass.getpass(prompt).strip()
    except Exception:
        return input(prompt).strip()


def _ask_bool(prompt: str, default: bool = True) -> bool:
    default_str = "Y/n" if default else "y/N"
    while True:
        try:
            val = input(f"{prompt} [{default_str}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not val:
            return default
        if val in ("y", "yes", "true"):
            return True
        if val in ("n", "no", "false"):
            return False
        print(f"  Please enter y or n.")


def run_setup_wizard() -> None:
    """Run the interactive first-time setup wizard."""
    print(f"{CLEAR_SCREEN}")
    print("=" * 60)
    print("  Welcome to ZetBot AI Setup Wizard")
    print("=" * 60)
    print()
    print("This wizard will help you configure your trading bot.")
    print("Press Enter to accept defaults where shown.")
    print()

    if env_exists() and env_is_valid():
        if not _ask_bool("Configuration already exists. Overwrite?", default=False):
            print("\nSetup cancelled.")
            return
        reset_env(backup=True)

    values: dict[str, str] = {}

    for field in CONFIG_FIELDS:
        print()
        if field.secret:
            print(f"  {field.label}:")
        value = _input(f"  {field.label}", field.default, secret=field.secret)
        values[field.key] = value

    print()
    print("-" * 60)
    print("  Configuration Summary")
    print("-" * 60)
    for field in CONFIG_FIELDS:
        raw = values.get(field.key, field.default)
        display = "******" if field.secret and raw else raw
        print(f"  {field.label:30s} = {display}")

    print()
    if _ask_bool("Save this configuration?", default=True):
        errors = validate_env_dict(values)
        if errors:
            print("\nValidation errors:")
            for e in errors:
                print(f"  - {e}")
            print("\nSetup failed. Please try again.")
            raise RuntimeError("Setup validation failed")

        write_env(values)
        print(f"\nConfiguration saved to {ENV_PATH}")
        print()
        print("Next steps:")
        print("  Run diagnostics:   python3 main.py --diagnostics")
        print("  Start bot:         python3 main.py")
        print("  Open menu:         python3 main.py --wizard")
    else:
        print("\nSetup cancelled. No changes made.")


def run_config_update() -> None:
    """Update individual configuration values interactively."""
    if not env_exists():
        print("No configuration found. Run setup first: python3 main.py --setup")
        return

    from scripts.config_manager import read_env
    current = read_env()

    print(f"{CLEAR_SCREEN}")
    print("=" * 60)
    print("  Update Configuration")
    print("=" * 60)
    print("  Leave blank to keep current value.")
    print()

    updated = dict(current)
    for field in CONFIG_FIELDS:
        cur = current.get(field.key, field.default)
        display = "******" if field.secret and cur else (cur or "(not set)")
        if field.secret:
            new_val = _input(f"  {field.label} [{display}]", "", secret=True)
        else:
            new_val = _input(f"  {field.label} [{display}]", cur)
        if new_val:
            updated[field.key] = new_val

    errors = validate_env_dict(updated)
    if errors:
        print("\nValidation errors:")
        for e in errors:
            print(f"  - {e}")
        return

    if not _ask_bool("Save these changes?", default=True):
        print("\nUpdate cancelled.")
        return

    write_env(updated)
    print("\nConfiguration updated.")
