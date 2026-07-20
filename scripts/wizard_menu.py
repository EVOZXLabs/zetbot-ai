"""
Interactive Wizard Menu for ZetBot AI.

Provides a menu-driven interface for all operations.
"""

import os
import sys


CLEAR = "\033[2J\033[H"


def _show_header() -> None:
    print(f"{CLEAR}")
    print("=" * 60)
    print("  ZetBot AI — Operations Wizard")
    print("=" * 60)
    print()


def _show_menu() -> None:
    print("  Main Menu:")
    print()
    print("    1.  Start Bot")
    print("    2.  Setup Wizard")
    print("    3.  Show Configuration")
    print("    4.  Update Configuration")
    print("    5.  Test Exchange Connection")
    print("    6.  Test Telegram Connection")
    print("    7.  Backup")
    print("    8.  Restore")
    print("    9.  Export Configuration")
    print("   10.  Import Configuration")
    print("   11.  Diagnostics")
    print("   12.  System Information")
    print("   13.  Update System")
    print()
    print("    0.  Exit")
    print()


def run_wizard_menu() -> None:
    """Run the interactive wizard menu loop."""
    while True:
        _show_header()
        _show_menu()

        try:
            choice = input("  Select option [0-13]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            sys.exit(0)

        if choice == "0":
            print("\n  Goodbye!")
            sys.exit(0)

        try:
            if choice == "1":
                _start_bot()
            elif choice == "2":
                _run_setup()
            elif choice == "3":
                _show_config()
            elif choice == "4":
                _update_config()
            elif choice == "5":
                _test_exchange()
            elif choice == "6":
                _test_telegram()
            elif choice == "7":
                _do_backup()
            elif choice == "8":
                _do_restore()
            elif choice == "9":
                _export_config()
            elif choice == "10":
                _import_config()
            elif choice == "11":
                _run_diagnostics()
            elif choice == "12":
                _show_system_info()
            elif choice == "13":
                _update_system()
            else:
                print("  Invalid option. Press Enter to continue.")
                input()
        except (EOFError, KeyboardInterrupt):
            print("\n  Operation cancelled.")

        input("\n  Press Enter to return to menu...")


def _start_bot() -> None:
    print(f"{CLEAR}")
    print("=" * 60)
    print("  Starting ZetBot AI...")
    print("=" * 60)
    print()
    print("  The bot will start and take over this terminal.")
    print("  Press Ctrl+C to stop the bot and return to the menu.")
    print()

    from scripts.startup_validator import validate_startup
    if not validate_startup():
        print("\n  Startup validation failed. Fix issues and try again.")
        print("  Press Enter to return to menu...")
        input()
        return

    try:
        from main import main
        main()
    except KeyboardInterrupt:
        print("\n  Bot stopped.")
    except Exception as exc:
        print(f"\n  Bot exited: {exc}")


def _run_setup() -> None:
    from scripts.setup_wizard import run_setup_wizard
    run_setup_wizard()


def _show_config() -> None:
    from scripts.config_manager import display_config
    print(f"{CLEAR}")
    print(display_config())


def _update_config() -> None:
    from scripts.setup_wizard import run_config_update
    run_config_update()


def _test_exchange() -> None:
    from scripts.exchange_test import run_exchange_test
    print(f"{CLEAR}")
    result = run_exchange_test()
    print(result)


def _test_telegram() -> None:
    from scripts.telegram_test import run_telegram_test
    print(f"{CLEAR}")
    result = run_telegram_test()
    print(f"\n=== Telegram Connection Test ===\n")
    print(f"  {result}")


def _do_backup() -> None:
    from scripts.backup_restore import create_backup
    print(f"{CLEAR}")
    print("=== Creating Backup ===\n")
    try:
        path = create_backup()
        print(f"  Backup created: {path}")
    except Exception as exc:
        print(f"  Backup failed: {exc}")


def _do_restore() -> None:
    from scripts.backup_restore import list_backups, restore_backup
    print(f"{CLEAR}")
    print("=== Available Backups ===\n")
    backups = list_backups()
    if not backups:
        print("  No backups found.")
        return
    for i, b in enumerate(backups, 1):
        print(f"  {i}. {b['filename']}  ({b['size']})  {b['valid']}")
    print()
    try:
        choice = input("  Select backup to restore [1-{0}]: ".format(len(backups))).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(backups):
            restore_backup(backups[idx]["path"])
        else:
            print("  Invalid selection.")
    except (EOFError, KeyboardInterrupt):
        print("\n  Restore cancelled.")
    except (ValueError, IndexError):
        print("  Invalid selection.")


def _export_config() -> None:
    from scripts.config_import_export import export_config
    print(f"{CLEAR}")
    print("=== Export Configuration ===\n")
    try:
        path = export_config(include_secrets=False)
        print(f"  Exported to: {path}")
    except Exception as exc:
        print(f"  Export failed: {exc}")


def _import_config() -> None:
    from scripts.config_import_export import import_config
    print(f"{CLEAR}")
    print("=== Import Configuration ===\n")
    path = input("  Path to config file: ").strip()
    if not path:
        print("  No path provided.")
        return
    import_config(path, force=False)


def _run_diagnostics() -> None:
    from scripts.diagnostics import run_diagnostics
    print(f"{CLEAR}")
    result = run_diagnostics()
    result.print_report()


def _show_system_info() -> None:
    from scripts.system_info import get_system_info
    print(f"{CLEAR}")
    info = get_system_info()
    print(info)


def _update_system() -> None:
    print(f"{CLEAR}")
    print("=== Update System ===\n")
    print("  This will pull the latest version from git and update dependencies.")
    print("  Repo: " + _git_remote_url())

    if not os.path.exists("requirements.txt"):
        print("  Error: requirements.txt not found. Cannot update dependencies.")
        return

    ans = input("  Continue? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        print("  Update cancelled.")
        return

    import subprocess

    try:
        git_dir = os.path.join(os.getcwd(), ".git")
        if not os.path.isdir(git_dir):
            print("  Error: not a git repository.")
            return

        r = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=60)
        print(f"  git pull: {r.stdout.strip()}")
        if r.returncode != 0:
            print(f"  git pull error: {r.stderr.strip()}")
            return
    except FileNotFoundError:
        print("  Error: git not found. Is git installed?")
        return
    except Exception as exc:
        print(f"  git pull failed: {exc}")
        return

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            capture_output=True, text=True, timeout=120,
        )
        print(f"  Dependencies: {'OK' if r.returncode == 0 else 'FAILED'}")
        if r.stderr.strip():
            for line in r.stderr.strip().split("\n")[-3:]:
                print(f"  {line}")
    except Exception as exc:
        print(f"  pip install failed: {exc}")


def _git_remote_url() -> str:
    try:
        import subprocess
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"
