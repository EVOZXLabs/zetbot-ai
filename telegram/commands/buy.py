from telegram.base_command import BaseCommand, CommandMeta


class BuyCommand(BaseCommand):
    meta = CommandMeta(
        name="buy",
        aliases=["long"],
        description="Manually open a position (admin only)",
        usage="/buy <symbol> [amount_usdt]",
        permission="admin",
        hidden=False,
    )

    def execute(self, ctx, args: str) -> str:
        if not args.strip():
            return (
                "\U0001f6ab *Buy*\n"
                "Usage: `/buy BTC/USDT 100`\n"
                "Creates a BUY order via OrderManager."
            )

        parts = args.strip().split()
        symbol = parts[0].upper()
        if not symbol.endswith("/USDT"):
            symbol = symbol.upper() + "/USDT"

        try:
            amount_usdt = float(parts[1]) if len(parts) > 1 else 0.0
        except (ValueError, IndexError):
            return "\u274c Invalid amount. Usage: `/buy BTC/USDT 100`"

        if amount_usdt <= 0:
            return "\u274c Invalid amount."

        if ctx.services is None:
            return "\u274c Services not available."

        # Get current price
        ticker = ctx.services.exchange.get_ticker(symbol)
        price = ticker.get("last") or ticker.get("ask") or 0.0
        if price <= 0:
            return f"\u274c Cannot determine price for {symbol}."

        quantity = amount_usdt / price

        from scripts.execution_engine import OrderRequest  # noqa: PLC0415
        request = OrderRequest(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            amount=quantity,
            price=price,
            metadata={"source": "telegram", "bypass_risk": True},
        )

        result = ctx.services.order.execute(request)

        from telegram.commands._order_status import format_order_outcome  # noqa: PLC0415
        message, should_sync = format_order_outcome("Buy", symbol, result)
        if should_sync:
            ctx.services.order.sync_position(result)
        return message
