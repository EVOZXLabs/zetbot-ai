from telegram.base_command import BaseCommand, CommandMeta


class SellCommand(BaseCommand):
    meta = CommandMeta(
        name="sell",
        aliases=["short", "close"],
        description="Manually close a position (admin only)",
        usage="/sell <symbol> [amount]",
        permission="admin",
        hidden=False,
    )

    def execute(self, ctx, args: str) -> str:
        if not args.strip():
            return (
                "\U0001f6ab *Sell*\n"
                "Usage: `/sell BTC/USDT`\n"
                "Closes an open position via OrderManager."
            )

        parts = args.strip().split()
        symbol = parts[0].upper()
        if not symbol.endswith("/USDT"):
            symbol = symbol.upper() + "/USDT"

        if ctx.services is None:
            return "\u274c Services not available."

        # Find position quantity
        positions = ctx.services.position.get_open_positions()
        position = next((p for p in positions if p.get("symbol") == symbol), None)
        if position is None:
            return f"\u274c No open position for {symbol}."

        quantity = position.get("remaining_qty", position.get("quantity", 0))
        if quantity <= 0:
            return f"\u274c Position {symbol} has no remaining quantity."

        from scripts.execution_engine import OrderRequest  # noqa: PLC0415
        request = OrderRequest(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            amount=quantity,
            metadata={"source": "telegram", "bypass_risk": True},
        )

        result = ctx.services.order.execute(request)
        if hasattr(result, "status"):
            status = result.status
            error = result.error
            filled = result.filled_amount
            fill_price = result.filled_price
        else:
            status = result.get("status", "UNKNOWN")
            error = result.get("error")
            filled = result.get("filled_amount", 0)
            fill_price = result.get("filled_price", 0)

        if status in ("FILLED", "EXECUTED"):
            ctx.services.order.sync_paper_state(result)
            return (
                f"\u2705 *Sell Executed*\n"
                f"Symbol: `{symbol}`\n"
                f"Amount: `{filled:.6f}`\n"
                f"Price: `{fill_price:.6f}`\n"
                f"Executor: `{getattr(result, 'executor', result.get('executor', '?'))}`"
            )
        return (
            f"\u274c *Sell Failed*\n"
            f"Symbol: `{symbol}`\n"
            f"Status: `{status}`\n"
            f"Error: `{error or 'Unknown'}`"
        )
