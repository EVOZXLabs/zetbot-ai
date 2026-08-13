from telegram.base_command import BaseCommand, CommandMeta


def _live_position(ctx, symbol: str) -> dict | None:
    """Fresh exchange-truth position for *symbol* (cached as fallback)."""
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
        return load_live_positions().get(symbol)
    except Exception:
        return None


def _parse_value(raw: str, entry: float, direction: str) -> float:
    """Parse ``10`` / ``10%`` (percent off entry) or an absolute price.

    ``direction`` is "sl" (percent subtracts from entry) or "tp"
    (percent adds to entry). A bare number is treated as percent, except
    when it is clearly a price (abs > 100, or within 50% of entry).
    """
    value = float(raw.strip().rstrip("%"))
    if value <= 0:
        raise ValueError("value must be positive")
    looks_like_price = (
        abs(value) > 100
        or (entry > 0 and abs(value - entry) < entry * 0.5)
    )
    if looks_like_price:
        return value
    if direction == "tp":
        return entry * (1 + value / 100.0)
    return entry * (1 - value / 100.0)


class StoplossCommand(BaseCommand):
    meta = CommandMeta(
        name="stoploss",
        aliases=["sl"],
        description="Set/update the stop-loss protection order for a LIVE "
                     "position (percent or absolute price)",
        usage="/stoploss <symbol> <pct|price>",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        parts = args.strip().split()
        if len(parts) < 2:
            return (
                "\U0001f6ab *Stop Loss*\n"
                "Usage: /stoploss <symbol> <pct|price>\n"
                "Example: /stoploss BTC/USDT 5  (SL 5% below entry)\n"
                "Example: /stoploss BTC/USDT 95000  (absolute price)"
            )
        if ctx.services is None:
            return "\u274c Services not available."

        order = ctx.services.order
        if order.mode != "LIVE":
            return "\u2139\ufe0f /stoploss applies to LIVE positions only."

        qc = (getattr(ctx.config, "quote_currency", None) or "USDT").upper()
        symbol = parts[0].upper()
        if not symbol.endswith(f"/{qc}"):
            symbol = symbol.upper() + f"/{qc}"

        position = _live_position(ctx, symbol)
        if position is None:
            return f"\u274c No open LIVE position for {symbol}."

        entry = position.get("entry_price") or 0.0
        if entry <= 0:
            return f"\u274c Cannot resolve entry price for {symbol}."

        try:
            stop_price = _parse_value(parts[1], entry, "sl")
        except (ValueError, TypeError):
            return "\u274c Invalid stop value."

        if stop_price >= entry:
            return (
                "\u274c Stop must be BELOW entry "
                f"({stop_price:.4f} >= entry {entry:.4f})."
            )

        try:
            from scripts.protection_manager import (  # noqa: PLC0415
                ProtectionManager,
            
            )

            pm = ProtectionManager(ctx.services.exchange, ctx.services.config)
            existing = pm.get_protection(symbol) or {}
            tp_price = existing.get("take_profit_price") or 0.0

            # Replacing protection means cancelling the current paired
            # orders first — otherwise a second pair would stack on the
            # exchange for the same position.
            try:
                pm.cancel_protection(symbol, reason="stoploss_command")
            except Exception:
                pass

            record = pm.create_protection(
                position,
                stop_price=stop_price,
                take_profit_price=tp_price if tp_price > 0 else None,
            )
            return (
                f"\U0001f6e1\ufe0f *Stop Loss updated — {symbol}*\n"
                f"Entry {entry:.4f} · SL {record['stop_price']} "
                f"({(record['stop_price'] - entry) / entry * 100:+.2f}%)\n"
                f"TP {record['take_profit_price']} · status {record['status']}"
            )
        except Exception as exc:
            return f"\u274c Protection update failed: `{exc}`"
