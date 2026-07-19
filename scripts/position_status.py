"""Canonical position-status vocabulary for ZetBot AI.

``positions.json`` (written by :mod:`scripts.position_manager`) uses a
richer status vocabulary than the simple OPEN/CLOSED used internally by
the paper engine's ``VirtualPosition``:

    OPEN | PARTIAL | TRAILING | BREAKEVEN   -> still holding exposure
    CLOSED | STOPPED | TIMEOUT              -> flat, no exposure

Every module that reads ``positions.json`` (or anything derived from it)
and needs to know whether a position still represents live market
exposure MUST import ``OPEN_STATUSES`` / ``is_open`` from here instead of
re-declaring its own ``status == "OPEN"`` check or its own tuple of
status strings.

This module exists because that exact divergence caused a production
incident (2026-07): several readers of ``positions.json`` (Telegram's
``MetricsManager``, ``/status``, ``/wallet``, ``/portfolio``, ``/sell``,
health checks) only matched literal ``"OPEN"`` and silently dropped
positions that had moved to ``BREAKEVEN`` or ``TRAILING`` after a partial
take-profit — even though those positions still had ``remaining_qty >
0`` and real exposure. Telegram reported "no open positions / 0%
exposure" while ``positions.json`` showed real, live positions.

Do not add a new inline status tuple anywhere else in the codebase.
Import from here.

Note: this module is specifically for ``positions.json`` /
``scripts.position_manager.Position`` records. The paper engine's own
``VirtualPosition`` (``paper_state.json``) uses a different, simpler
OPEN/CLOSED vocabulary and correctly compares against ``"OPEN"``
directly — it is not affected by this module.
"""

from __future__ import annotations

# Statuses that mean "this position still holds market exposure".
# remaining_qty > 0 is expected to be true for all of these.
OPEN_STATUSES: frozenset[str] = frozenset({
    "OPEN",
    "PARTIAL",
    "TRAILING",
    "BREAKEVEN",
})

# Statuses that mean "flat, no exposure remaining".
CLOSED_STATUSES: frozenset[str] = frozenset({
    "CLOSED",
    "STOPPED",
    "TIMEOUT",
})


def is_open(status: str | None) -> bool:
    """Return True if a positions.json-style status still holds exposure."""
    return status in OPEN_STATUSES
