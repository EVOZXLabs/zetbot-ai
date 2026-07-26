from telegram.base_command import CommandMeta
from telegram.commands.wallet import WalletCommand


class BalanceCommand(WalletCommand):
    """``/balance`` — the detailed version of ``/wallet``.

    Reuses WalletCommand's data gathering (``_data()``) so the numbers
    can never drift from /wallet's — it only adds the full cash / P&L /
    exposure breakdown that /wallet keeps hidden for a quicker glance.
    """

    meta = CommandMeta(
        name="balance",
        aliases=["bal", "equity"],
        description="Full balance breakdown — cash, P&L, exposure",
        usage="/balance",
        permission="user",
    )

    show_breakdown = True
