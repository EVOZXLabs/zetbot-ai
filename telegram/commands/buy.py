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
                "Usage: /buy <symbol> <amount>\n"
                "Creates a BUY order via OrderManager."
            )

        parts = args.strip().split()
        symbol = parts[0].upper()
        qc = (getattr(ctx.config, "quote_currency", None) or "USDT").upper()
        if not symbol.endswith(f"/{qc}"):
            symbol = symbol.upper() + f"/{qc}"

        try:
            amount_usdt = float(parts[1]) if len(parts) > 1 else 0.0
        except (ValueError, IndexError):
            return "\u274c Invalid amount. Usage: /buy <symbol> <amount>"

        if amount_usdt <= 0:
            return "\u274c Invalid amount."

        if ctx.services is None:
            return "\u274c Services not available."

        # Hard rule (AGENTS.md): never place multiple positions for the
        # same symbol simultaneously. Reject the manual BUY when one is
        # already open — live reads live_positions.json (exchange truth),
        # paper reads the paper position ledger.
        if ctx.services.order.mode == "LIVE":
            open_pos = self._find_live_open_position(ctx, symbol)
        else:
            open_pos = next(
                (p for p in ctx.services.position.get_open_positions()
                 if p.get("symbol") == symbol),
                None,
            )
        if open_pos is not None:
            return (
                f"\u274c A position for {symbol} is already open. "
                "Close it first (/sell) before opening another."
            )

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

        if ctx.services.order.mode == "LIVE" and should_sync:
            record = ctx.services.order.get_protection_status(symbol)
            if record is None:
                message += (
                    "\n\n\u26a0\ufe0f *No protection order attempted* "
                    "(AUTO_PROTECT is off, or no order id to attach to)."
                )
            elif record.get("status") == "ACTIVE":
                message += (
                    f"\n\n\U0001f6e1\ufe0f Protection: SL `{record['stop_price']}` "
                    f"/ TP `{record['take_profit_price']}` — ACTIVE"
                )
            elif record.get("status") in ("ERROR", "PARTIAL_ERROR"):
                message += (
                    f"\n\n\u274c *Protection FAILED*: {record.get('error')}\n"
                    "This position may be UNPROTECTED — check manually."
                )

        return message

    @staticmethod
    def _find_live_open_position(ctx, symbol: str) -> dict | None:
        """True when the exchange currently holds an open position for
        *symbol* (fresh sync first, cached record as fallback)."""
        try:
            from scripts.live_position_sync import (  # noqa: PLC0415
                LivePositionSync,
                load_live_positions,
            )

            quote = getattr(ctx.services.config, "quote_currency", "USDT") or "USDT"
            try:
                syncer = LivePositionSync(ctx.services.exchange, quote_currency=quote)
                for p in syncer.sync_positions([symbol]):
                    if p.get("symbol") == symbol:
                        return p
            except Exception:
                pass
            cached = load_live_positions()
            return cached.get(symbol)
        except Exception:
            return None
