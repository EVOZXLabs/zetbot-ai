"""Single place to resolve ``initial_balance`` for return-pct calculations.

Bug this fixes
──────────────
``paper_balance.json``, ``telegram/commands/balance.py`` and
``telegram/commands/wallet.py`` each independently fell back to a
hardcoded ``10_000.0`` whenever ``initial_balance`` was missing from
``paper_balance.json``. If the account was actually funded with a
different amount (e.g. $6,000), the fallback silently produced a
``total_return_pct`` computed against the wrong base — which is why
``/balance`` could show a large negative "Return" next to a positive
"Net PnL": the two numbers were computed from different baselines.

``paper_state.json`` is the wallet's own record of how it was funded
(see ``scripts/accounting_reconcile.py``, which already treats it as
authoritative when reconciling ``paper_balance.json`` at startup), so
it is the correct fallback — a hardcoded literal should only be used
if neither file has the value.
"""

from __future__ import annotations

from typing import Any

_FALLBACK_INITIAL_BALANCE = 10_000.0


def resolve_initial_balance(
    paper_balance: dict[str, Any],
    paper_state: dict[str, Any] | None = None,
) -> float:
    """Return the correct ``initial_balance`` to use for return-pct math.

    Priority:
      1. ``paper_balance.json``'s own ``initial_balance``, if present.
      2. ``paper_state.json``'s ``initial_balance`` (authoritative wallet
         record), if present.
      3. The historical hardcoded default, only as a last resort.
    """
    if "initial_balance" in paper_balance:
        return float(paper_balance["initial_balance"])
    if paper_state and "initial_balance" in paper_state:
        return float(paper_state["initial_balance"])
    return _FALLBACK_INITIAL_BALANCE
