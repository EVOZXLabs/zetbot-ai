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
                "Usage: /sell <symbol> [amount]\n"
                "Closes an open position via OrderManager."
            )

        parts = args.strip().split()
        symbol = parts[0].upper()
        qc = (getattr(ctx.config, "quote_currency", None) or "USDT").upper()
        if not symbol.endswith(f"/{qc}"):
            symbol = symbol.upper() + f"/{qc}"

        if ctx.services is None:
            return "\u274c Services not available."

        live = getattr(ctx.services.order, "mode", "PAPER") == "LIVE"

        # Find position quantity. In LIVE mode the position must come from
        # live_positions.json (the exchange-truth record) — positions.json
        # is the PAPER simulation ledger and can hold stale/wrong
        # quantities that would make /sell place a wrong-sized real order.
        position = None
        if live:
            position = self._find_live_position(ctx, symbol)
        else:
            positions = ctx.services.position.get_open_positions()
            position = next(
                (p for p in positions if p.get("symbol") == symbol), None,
            )
        if position is None:
            from scripts.live_position_sync import parse_exclude_symbols  # noqa: PLC0415
            exclude = getattr(ctx.services.config, "exclude_symbols", "") or ""
            if symbol.split("/")[0].upper() in parse_exclude_symbols(exclude):
                return (
                    f"\u274c {symbol} is on EXCLUDE_SYMBOLS — the bot won't "
                    "touch it. Sell it directly on the exchange, or remove "
                    "it from EXCLUDE_SYMBOLS if you want the bot to manage it."
                )
            return f"\u274c No open position for {symbol}."

        quantity = position.get("remaining_qty", position.get("quantity", 0))
        if quantity <= 0:
            return f"\u274c Position {symbol} has no remaining quantity."

        # Optional partial exit: /sell SYMBOL <amount> sells at most
        # <amount> base units (validated by the executor against the
        # actual position size).
        if len(parts) > 1:
            try:
                requested = float(parts[1])
            except (ValueError, TypeError):
                return (
                    "\u274c Invalid amount. Usage: /sell <symbol> [amount]"
                )
            if requested > 0:
                quantity = min(requested, quantity)

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
                entry_time = position.get("entry_time") or position.get("opened_at", "")
                holding = timedelta()
                if entry_time:
                    try:
                        dt = datetime.fromisoformat(entry_time.split("+")[0].split("Z")[0])
                        holding = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
                        if holding.total_seconds() < 0:
                            holding = timedelta()
                    except (ValueError, TypeError):
                        pass
                exit_price = getattr(result, "filled_price", 0) or position.get("current_price", 0)
                ctx.services.notification.notify_close(
                    symbol=symbol,
                    pnl=position.get("total_pnl", 0),
                    reason="Manual Close",
                    exit_price=exit_price,
                )
            except Exception:
                pass

        return message

    @staticmethod
    def _find_live_position(ctx, symbol: str) -> dict | None:
        """Resolve an open LIVE position for *symbol* (exchange truth)."""
        try:
            from scripts.live_position_sync import (  # noqa: PLC0415
                LivePositionSync,
                load_live_positions,
            )
            from scripts.exchange_providers import ExchangeAuthError  # noqa: PLC0415

            quote = getattr(ctx.services.config, "quote_currency", "USDT") or "USDT"
            exclude = getattr(ctx.services.config, "exclude_symbols", "") or ""
            # Fresh sync first (single symbol), cached record as fallback.
            try:
                syncer = LivePositionSync(ctx.services.exchange, quote_currency=quote, exclude_symbols=exclude)
                fresh = syncer.sync_positions([symbol])
                for p in fresh:
                    if p.get("symbol") == symbol:
                        return p
            except Exception:
                pass
            cached = load_live_positions()
            return cached.get(symbol)
        except Exception:
            return None
