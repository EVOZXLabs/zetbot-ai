"""Shared order-outcome formatting for /buy and /sell.

NOT a command module — the leading underscore keeps
``telegram.registry.CommandRegistry.discover()`` from picking this up
as a command (it explicitly skips modules starting with "_").

After Order Reconciliation, a LIVE order result coming back from
``OrderManager.execute()`` is no longer just "FILLED or failed" — it
can honestly be PENDING, PARTIALLY_FILLED, or CANCELLED too, and each
of those means something different to the operator:

    FILLED / EXECUTED  -> done, real position exists
    PARTIALLY_FILLED   -> some of it filled, some is still open
    PENDING            -> exchange accepted it, not confirmed yet
                          (includes a reconciliation timeout)
    CANCELLED          -> exchange or user cancelled it
    anything else       -> REJECTED / FAILED — genuinely didn't happen

Collapsing all of these into a blanket "FAILED" message would tell the
operator an order didn't happen when it may have partially (or fully)
landed on the exchange — dangerous for a live account.
"""

from typing import Any


def _unpack(result: Any) -> dict[str, Any]:
    if hasattr(result, "status"):
        return {
            "status": result.status,
            "error": result.error,
            "filled_amount": result.filled_amount or 0.0,
            "filled_price": result.filled_price or 0.0,
            "executor": getattr(result, "executor", "") or "?",
            "amount": result.amount or 0.0,
        }
    return {
        "status": result.get("status", "UNKNOWN"),
        "error": result.get("error"),
        "filled_amount": result.get("filled_amount", 0.0) or 0.0,
        "filled_price": result.get("filled_price", 0.0) or 0.0,
        "executor": result.get("executor", "?"),
        "amount": result.get("amount", 0.0) or 0.0,
    }


def format_order_outcome(action: str, symbol: str, result: Any) -> tuple[str, bool]:
    """Build the Telegram reply for a /buy or /sell result.

    Returns ``(message, should_sync_position)`` — ``should_sync_position``
    tells the caller whether a real fill happened and local state should
    be updated (FILLED and PARTIALLY_FILLED both count; a plain PENDING
    or a clean CANCELLED with zero fill does not).

    ``action`` is a display label, e.g. "Buy" or "Sell".
    """
    r = _unpack(result)
    status = r["status"]
    filled = r["filled_amount"]
    price = r["filled_price"]
    requested = r["amount"]
    executor = r["executor"]
    error = r["error"]

    if status in ("FILLED", "EXECUTED"):
        return (
            f"\u2705 *{action} Success*\n"
            f"Symbol: `{symbol}`\n"
            f"Amount: `{filled:.6f}`\n"
            f"Price: `{price:.6f}`\n"
            f"Executor: `{executor}`"
        ), True

    if status == "PARTIALLY_FILLED":
        return (
            f"\u26a0\ufe0f *{action} Partial*\n"
            f"Symbol: `{symbol}`\n"
            f"Filled: `{filled:.6f}` / `{requested:.6f}` requested\n"
            f"Price: `{price:.6f}`\n\n"
            "Not a failure — part of the order filled, the remaining "
            "quantity may still be open on the exchange. Check "
            "/livecheck or the exchange directly for the final state."
        ), True

    if status == "PENDING":
        detail = f"\n\n{error}" if error else ""
        return (
            f"\u23f3 *{action} Pending*\n"
            f"Symbol: `{symbol}`\n"
            f"Requested: `{requested:.6f}`\n\n"
            "The exchange accepted the order but it isn't confirmed "
            f"filled yet — this is NOT a failure.{detail}\n\n"
            "Check the exchange or try again shortly before assuming "
            "anything; do not resubmit blindly."
        ), False

    if status == "CANCELLED":
        reason = f"\nReason: {error}" if error else ""
        return (
            f"\u26a0\ufe0f *{action} Cancelled*\n"
            f"Symbol: `{symbol}`\n"
            f"Filled before cancel: `{filled:.6f}`{reason}"
        ), filled > 0

    # REJECTED / FAILED / anything else we don't specifically recognize —
    # the only bucket that genuinely means "did not happen".
    return (
        f"\u274c *{action} Failed*\n"
        f"Symbol: `{symbol}`\n"
        f"Status: `{status}`\n"
        f"Error: `{error or 'Unknown'}`"
    ), False
