"""
Configuration Import and Export for ZetBot AI.

Export configuration to JSON (with secrets masked by default).
Import configuration from JSON with validation.
Optional password-protected encryption.
"""

import json
import os
from typing import Optional

from scripts.config_manager import (
    CONFIG_FIELDS,
    ENV_PATH,
    MASK,
    config_to_dict,
    env_exists,
    validate_env_dict,
    write_env,
)

VERSION = "v0.7.2"
EXPORT_PATH = "zetbot-config.json"


def export_config(include_secrets: bool = False, password: Optional[str] = None) -> str:
    """Export configuration to JSON file. Returns the file path."""
    if not env_exists():
        raise RuntimeError("No .env file found. Run setup first.")

    raw = config_to_dict()
    if not include_secrets:
        for field in CONFIG_FIELDS:
            if field.secret and raw.get(field.key):
                raw[field.key] = MASK

    data = {
        "version": VERSION,
        "exported_at": __import__("datetime").datetime.now().isoformat(),
        "config": raw,
    }

    if password:
        try:
            from cryptography.fernet import Fernet
            import base64
            import hashlib
            salt = os.urandom(16)
            kdf = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
            key = base64.urlsafe_b64encode(kdf)
            cipher = Fernet(key)
            payload = json.dumps(data).encode()
            encrypted = salt + cipher.encrypt(payload)
            with open(EXPORT_PATH, "wb") as f:
                f.write(encrypted)
        except ImportError:
            raise RuntimeError(
                "cryptography package is required for password-protected export. "
                "Install it with: pip install cryptography"
            )
    else:
        _write_json(EXPORT_PATH, data)

    return EXPORT_PATH


def _write_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def import_config(path: str, password: Optional[str] = None, force: bool = False) -> bool:
    """Import configuration from a JSON file. Returns True on success."""
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return False

    data: dict = {}
    try:
        if password:
            try:
                from cryptography.fernet import Fernet
                import base64
                import hashlib
                with open(path, "rb") as f:
                    raw = f.read()
                salt = raw[:16]
                encrypted = raw[16:]
                kdf = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
                key = base64.urlsafe_b64encode(kdf)
                cipher = Fernet(key)
                decrypted = cipher.decrypt(encrypted)
                data = json.loads(decrypted.decode())
            except Exception:
                print("Decryption failed. Wrong password or corrupted file.")
                return False
        else:
            with open(path) as f:
                data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Failed to read config file: {exc}")
        return False

    config = data.get("config", {})
    if not config:
        print("No configuration found in file.")
        return False

    errors = validate_env_dict(config)
    if errors:
        print("Validation errors in imported config:")
        for e in errors:
            print(f"  - {e}")
        if not force:
            print("Use --force to import despite errors.")
            return False

    if env_exists() and not force:
        if not sys.stdin.isatty():
            print("Import cancelled: non-interactive mode and --force not used.")
            return False
        ans = input("Configuration already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Import cancelled.")
            return False

    write_env(config)
    print(f"Configuration imported from {path}")
    return True


def show_import_export_help() -> str:
    return (
        "Export:  python3 main.py --export-config [--include-secrets] [--password <pass>]\n"
        "Import:  python3 main.py --import-config <file.json> [--password <pass>] [--force]"
    )
