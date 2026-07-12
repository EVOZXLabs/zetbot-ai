"""LIVE trading arm/disarm commands — Phase 5 safety switch.

Flow (deliberately 3 steps, never auto-armed):

    /livecheck   -> read-only diagnostic, arms nothing
    /golive      -> if ready, shows a REAL-MONEY warning and asks for
                    an explicit reply
    CONFIRM LIVE -> only THIS reply, within the confirmation window,
                    actually calls OrderManager.arm_live()

A process restart NEVER re-arms live trading by itself, even if
data/live_armed.json shows ``armed: true`` from a previous session —
see the startup check in main.py. The operator must always run
/golive + CONFIRM LIVE again after every restart.
"""

import time
from typing import Any, Optional

from telegram.base_command import BaseCommand, CommandMeta
from scripts.order_manager import LiveArmError

CONFIRM_WINDOW_SEC = 120.0


def _format_balance(balance: Optional[float], currency: str) -> str:
    if isinstance(balance, (int, float)):
        return f"{balance:,.2f} {currency}"
    return "Unknown"


def _format_permission(can_trade: Optional[bool]) -> str:
    if can_trade is True:
        return "OK"
    if can_trade is False:
        return "BLOCKED"
    return "Unknown (not reported by exchange)"


def _format_check(report: dict[str, Any]) -> str:
    ready = report.get("ready")
    icon = "\U0001f7e2" if ready else "\U0001f534"
    lines = [f"{icon} *LIVE CHECK*", ""]
    lines.append(f"Exchange:\n{report.get('exchange') or '-'}")
    lines.append("")
    lines.append(
        f"Balance:\n{_format_balance(report.get('balance'), report.get('currency', ''))}",
    )
    lines.append("")
    lines.append(
        f"API:\n{'Connected' if report.get('connected') else 'NOT connected'}",
    )
    lines.append("")
    lines.append(
        f"Trading Permission:\n{_format_permission(report.get('trading_permission'))}",
    )

    if report.get("reasons"):
        lines.append("")
        lines.append("Issues:")
        for reason in report["reasons"]:
            lines.append(f"- {reason}")

    lines.append("")
    lines.append(f"Status:\n{'READY TO ARM' if ready else 'NOT READY'}")
    return "\n".join(lines)


class LiveCheckCommand(BaseCommand):
    meta = CommandMeta(
        name="livecheck",
        description="Check whether LIVE trading is ready to be armed (read-only)",
        usage="/livecheck",
        permission="admin",
    )

    def execute(self, ctx: Any, args: str) -> str:
        if ctx.services is None:
            return "\u26a0\ufe0f Service container not available."
        report = ctx.services.order.live_readiness_report()
        return _format_check(report)


class GoLiveCommand(BaseCommand):
    meta = CommandMeta(
        name="golive",
        description="Begin arming LIVE (real-money) trading — asks for confirmation",
        usage="/golive",
        permission="admin",
    )

    def execute(self, ctx: Any, args: str) -> str:
        if ctx.services is None:
            return "\u26a0\ufe0f Service container not available."

        order = ctx.services.order
        if order.is_live_enabled():
            return (
                "\u2139\ufe0f Live trading is already ARMED. "
                "No confirmation needed."
            )

        report = order.live_readiness_report()
        if not report.get("ready"):
            return "\U0001f534 *Cannot arm live trading yet*\n\n" + _format_check(report)

        ConfirmLiveCommand.register_pending(str(ctx.chat_id))

        return (
            "\u26a0\ufe0f *WARNING*\n\n"
            "You are enabling REAL MONEY trading.\n\n"
            f"Exchange:\n{report.get('exchange')}\n\n"
            f"Balance:\n{_format_balance(report.get('balance'), report.get('currency', ''))}\n\n"
            "Reply:\n`CONFIRM LIVE`\n\n"
            f"_(expires in {int(CONFIRM_WINDOW_SEC)}s — run /golive again if it does)_"
        )


class ConfirmLiveCommand(BaseCommand):
    """Handles the literal reply ``CONFIRM LIVE``.

    Not meant to be discovered via /help (hidden) — it only does
    anything useful as a direct reply right after /golive.
    """

    meta = CommandMeta(
        name="confirm",
        aliases=["confirm_live"],
        description="Confirm arming live trading (reply to /golive)",
        usage="CONFIRM LIVE",
        permission="admin",
        hidden=True,
    )

    # chat_id -> timestamp of the /golive that's awaiting confirmation
    _pending: dict[str, float] = {}

    @classmethod
    def register_pending(cls, chat_key: str) -> None:
        cls._pending[chat_key] = time.time()

    def execute(self, ctx: Any, args: str) -> str:
        if args.strip().upper() != "LIVE":
            return (
                "\u2139\ufe0f To arm live trading: run /golive first, then "
                "reply with exactly `CONFIRM LIVE`."
            )

        if ctx.services is None:
            return "\u26a0\ufe0f Service container not available."

        key = str(ctx.chat_id)
        requested_at = self._pending.get(key)
        now = time.time()
        if not requested_at or (now - requested_at) > CONFIRM_WINDOW_SEC:
            self._pending.pop(key, None)
            return (
                "\u26a0\ufe0f No pending LIVE confirmation (or it expired). "
                "Run /golive again."
            )

        order = ctx.services.order

        # Re-check readiness right before arming — conditions (balance,
        # connectivity) may have changed in the seconds since /golive.
        report = order.live_readiness_report()
        if not report.get("ready"):
            self._pending.pop(key, None)
            return (
                "\U0001f534 *Aborted* — live trading is no longer ready:\n\n"
                + _format_check(report)
            )

        # arm_live() itself re-validates one more time immediately before
        # flipping the switch (defense in depth against the tiny window
        # between the check above and this call) — it will raise instead
        # of arming if that final check fails.
        try:
            record = order.arm_live(chat_id=ctx.chat_id)
        except LiveArmError as exc:
            # Expected outcome: readiness genuinely failed at the last
            # moment (API revoked, balance changed, permission changed).
            self._pending.pop(key, None)
            return f"\U0001f534 *Aborted* — readiness failed at the final check: {exc}"
        except Exception as exc:
            # Unexpected: a bug, import error, or infra failure — not a
            # readiness rejection. Flag it differently so it gets looked
            # at as a system problem, not just "conditions weren't ready".
            self._pending.pop(key, None)
            return f"\u26a0\ufe0f *System error while arming* — check logs: {exc}"

        self._pending.pop(key, None)

        return (
            "\U0001f7e2 *LIVE TRADING ARMED*\n\n"
            f"Exchange: {record.get('exchange')}\n"
            f"Time: {record.get('time')}\n\n"
            "Real orders will now be submitted to the exchange. "
            "Restarting the bot will require this confirmation again."
        )
