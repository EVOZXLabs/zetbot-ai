"""Manual trigger for protection-order reconciliation.

ProtectionManager.reconcile_all() (the bot's own "OCO" logic — cancel
the sibling leg once one side fills) IS wired into a background
scheduler — see ``scripts/protection_scheduler.py``, started from
``main.py`` whenever the engine is in LIVE mode, polling every
``config.protection_reconcile_interval_seconds`` (default 8s). This
command exists for an on-demand/manual pass (e.g. right after
suspecting a fill), it is not the only way reconciliation runs.
"""

from telegram.base_command import BaseCommand, CommandMeta


class ProtectionCheckCommand(BaseCommand):
    meta = CommandMeta(
        name="protectioncheck",
        aliases=["checkprotection"],
        description="Reconcile live SL/TP protection orders now (cancels the "
                     "filled sibling); also lists any unprotected live positions",
        usage="/protectioncheck",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        if ctx.services is None:
            return "\u26a0\ufe0f Service container not available."

        order = ctx.services.order
        if order.mode != "LIVE":
            return "\u2139\ufe0f Not in LIVE mode — nothing to reconcile."

        results = order.reconcile_all_protections()
        unprotected = order.find_unprotected_live_positions()

        lines = ["\U0001f6e1\ufe0f *Protection Check*", ""]

        if not results:
            lines.append("No ACTIVE protection records tracked.")
        else:
            for symbol, record in results.items():
                status = record.get("status") if record else "UNKNOWN"
                lines.append(f"{symbol}: {status}")

        if unprotected:
            lines.append("")
            lines.append("\u26a0\ufe0f *Unprotected live positions:*")
            for pos in unprotected:
                lines.append(
                    f"- {pos.get('symbol')} "
                    f"(qty {pos.get('quantity')}, entry "
                    f"{pos.get('entry_price', 'unknown')})"
                )

        return "\n".join(lines)
