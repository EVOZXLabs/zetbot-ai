from telegram.base_command import CommandMeta
from telegram.commands.wallet import WalletCommand


class BalanceCommand(WalletCommand):
    """``/balance`` — kept as a familiar alias name for ``/wallet``.

    Renders through the exact same WalletCommand.execute() so the two
    commands can never drift into two different formats for the same
    overlapping account data. Only the command name/aliases differ.
    """

    meta = CommandMeta(
        name="balance",
        aliases=["bal", "equity"],
        description="Your account balance, PnL and exposure (same as /wallet)",
        usage="/balance",
        permission="user",
    )
