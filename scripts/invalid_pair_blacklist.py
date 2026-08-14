"""Persistent blacklist for symbols the exchange itself rejects as an
invalid/untradeable pair (``data/invalid_pairs.json``).

Exchange market metadata (``load_markets()``) can lag reality: Indodax
may list a pair as ``active`` while its trading API still rejects orders
for it with ``"Invalid pair."``. Retrying such a symbol every pipeline
cycle burns rate limits and produces log noise for something that will
never succeed. This module lets the live executor remember a rejection
across restarts and lets the scanner skip the symbol as a candidate
going forward.

This is a soft, self-healing blacklist, not a permanent ban: entries
expire after ``_BLACKLIST_TTL_DAYS`` so a pair that gets relisted later
is naturally retried. Best-effort throughout — a blacklist read/write
failure must never interrupt trading.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

INVALID_PAIRS_PATH = "data/invalid_pairs.json"

# How long a symbol stays blacklisted before it's eligible to be tried
# again (the exchange may relist it, or the earlier rejection may have
# been transient).
BLACKLIST_TTL_DAYS = 14
_BLACKLIST_TTL_DAYS = BLACKLIST_TTL_DAYS  # internal alias used below

# Substrings of an exchange error that mean "this pair will never trade,
# don't retry it" as opposed to a transient failure (timeout, insufficient
# balance, precision, rate limit, etc.) that should NOT be blacklisted.
_INVALID_PAIR_MARKERS = (
    "invalid pair",
    "unknown symbol",
    "unknown pair",
    "market does not exist",
    "symbol not found",
)


def is_invalid_pair_error(error_text: str) -> bool:
    """True when an exception message indicates a permanently-untradeable
    pair rather than a transient order failure."""
    if not error_text:
        return False
    text = error_text.lower()
    return any(marker in text for marker in _INVALID_PAIR_MARKERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, suffix=".json.tmp", prefix=".atomic_",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _load() -> dict[str, Any]:
    try:
        with open(INVALID_PAIRS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _atomic_write_json(INVALID_PAIRS_PATH, data)
    except Exception:
        pass


def _is_expired(entry: dict[str, Any]) -> bool:
    ts = entry.get("blacklisted_at", "")
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    age = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
    return age.total_seconds() > BLACKLIST_TTL_DAYS * 86400


def blacklist_symbol(symbol: str, reason: str = "") -> None:
    """Record ``symbol`` as untradeable. Best-effort: never raises."""
    try:
        if not symbol:
            return
        data = _load()
        data[symbol] = {"blacklisted_at": _now(), "reason": reason or "Invalid pair"}
        _save(data)
    except Exception:
        pass


def is_blacklisted(symbol: str) -> bool:
    """True if ``symbol`` is currently blacklisted (and not expired).

    An expired entry is treated as not-blacklisted here but is only
    actually pruned from the file by ``load_active_blacklist``/cleanup,
    keeping this check cheap and side-effect-free.
    """
    try:
        entry = _load().get(symbol)
        if entry is None:
            return False
        return not _is_expired(entry)
    except Exception:
        return False


def load_active_blacklist() -> set[str]:
    """All currently-blacklisted (non-expired) symbols, pruning expired
    ones from disk as a side effect. Best-effort: returns an empty set
    on any failure rather than raising."""
    try:
        data = _load()
        active = {sym: e for sym, e in data.items() if not _is_expired(e)}
        if len(active) != len(data):
            _save(active)
        return set(active.keys())
    except Exception:
        return set()
