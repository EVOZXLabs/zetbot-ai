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
                "Usage: /sell <symbol>\n"
                "Closes an open position via OrderManager."
            )

        parts = args.strip().split()
        symbol = parts[0].upper()
        qc = (getattr(ctx.config, "quote_currency", None) or "USDT").upper()
        if not symbol.endswith(f"/{qc}"):
            symbol = symbol.upper() + f"/{qc}"

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
        price = position.get("current_price") or position.get("entry_price") or 0.0
        request = OrderRequest(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            amount=quantity,
            price=price if price > 0 else None,
            metadata={"source": "telegram", "bypass_risk": True},
        )

        result = ctx.services.order.execute(request)

        from telegram.commands._order_status import format_order_outcome  # noqa: PLC0415
        message, should_sync = format_order_outcome("Sell", symbol, result)
        if should_sync:
            ctx.services.order.sync_position(result)

        if result.status in ("FILLED", "EXECUTED"):
            try:
                from datetime import datetime, timezone, timedelta
                pos = next(
                    (p for p in positions if p.get("symbol") == symbol),
                    None,
                )
                if pos is not None:
                    entry_time = pos.get("entry_time") or pos.get("opened_at", "")
                    holding = timedelta()
                    if entry_time:
                        try:
                            dt = datetime.fromisoformat(entry_time.split("+")[0].split("Z")[0])
                            holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
                            if holding.total_seconds() < 0:
                                holding = timedelta()
                        except (ValueError, TypeError):
                            pass
                    exit_price = getattr(result, "filled_price", 0) or pos.get("current_price", 0)
                    ctx.services.notification.notify_close(
                        symbol=symbol,
                        pnl=pos.get("total_pnl", 0),
                        reason="Manual Close",
                        exit_price=exit_price,
                    )
            except Exception:
                pass

        return message
