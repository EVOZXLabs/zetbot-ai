import time
from typing import Any, Callable

from telegram.permissions import is_authorized


def _log(logger: Any, level: str, msg: str) -> None:
    """Adapter: handle both callable loggers and logger objects."""
    if logger is None:
        return
    if hasattr(logger, level):
        getattr(logger, level)(msg)
    elif callable(logger):
        logger(f"[{level.upper()}] {msg}")


def run_middleware(
    ctx: Any,
    command_name: str,
    execute_fn: Callable[[], str],
    logger: Any = None,
) -> str | None:
    """Run middleware pipeline before command execution.

    Returns None to continue, or a string response to abort.
    """

    # 1. Authorization
    if not is_authorized(ctx):
        return None  # silently ignore

    # 2. Cooldown check (skip in test mode)
    if not getattr(ctx, 'test_mode', False):
        now = time.time()
        key = (getattr(ctx, 'chat_id', ''), command_name)
        last_call = _cooldowns.get(key, 0.0)
        remaining = 1.0 - (now - last_call)
        if remaining > 0:
            _log(logger, "debug", f"Cooldown active for /{command_name}: {remaining:.1f}s")
            return None
        _cooldowns[key] = now

    # 3. Execute
    t0 = time.time()
    try:
        response = execute_fn()
        elapsed = time.time() - t0
        _log(logger, "info", f"Command /{command_name} executed in {elapsed:.2f}s")
        return response
    except Exception as exc:
        elapsed = time.time() - t0
        _log(logger, "error", f"Command /{command_name} failed after {elapsed:.2f}s: {exc}")
        return f"Command `/{command_name}` failed: `{exc}`"


_cooldowns: dict[tuple[str, str], float] = {}
