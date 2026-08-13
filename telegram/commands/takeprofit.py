from telegram.base_command import BaseCommand, CommandMeta
from telegram.commands.stoploss import _live_position, _parse_value


class TakeprofitCommand(BaseCommand):
    meta = CommandMeta(
        name="takeprofit",
        aliases=["tp"],
        description="Set/update the take-profit protection order for a LIVE "
                     "position (percent or absolute price)",
        usage="/takeprofit <symbol> <pct|price>",
        permission="admin",
        hidden=True,
    )

    def execute(self, ctx, args: str) -> str:
        parts = args.strip().split()
        if len(parts) < 2:
            return (
                "\U0001f3af *Take Profit*\n"
                "Usage: /takeprofit <symbol> <pct|price>\n"
                "Example: /takeprofit BTC/USDT 6  (TP 6% above entry)\n"
                "Example: /takeprofit BTC/USDT 108000  (absolute price)"
            )
        if ctx.services is None:
            return "\u274c Services not available."

        order = ctx.services.order
        if order.mode != "LIVE":
            return "\u2139\ufe0f /takeprofit applies to LIVE positions only."

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
            tp_price = _parse_value(parts[1], entry, "tp")
        except (ValueError, TypeError):
            return "\u274c Invalid take-profit value."

        if tp_price <= entry:
            return (
                "\u274c Take-profit must be ABOVE entry "
                f"({tp_price:.4f} <= entry {entry:.4f})."
            )

        try:
            from scripts.protection_manager import (  # noqa: PLC0415
                ProtectionManager,
            
            )

            pm = ProtectionManager(ctx.services.exchange, ctx.services.config)
            existing = pm.get_protection(symbol) or {}
            sl_price = existing.get("stop_price") or 0.0

            # Replace the current pair (cancel-then-create) so a second
            # protection pair never stacks on the exchange.
            try:
                pm.cancel_protection(symbol, reason="takeprofit_command")
            except Exception:
                pass

            record = pm.create_protection(
                position,
                stop_price=sl_price if sl_price > 0 else None,
                take_profit_price=tp_price,
            )
            return (
                f"\U0001f3af *Take Profit updated — {symbol}*\n"
                f"Entry {entry:.4f} · TP {record['take_profit_price']} "
                f"({(record['take_profit_price'] - entry) / entry * 100:+.2f}%)\n"
                f"SL {record['stop_price']} · status {record['status']}"
            )
        except Exception as exc:
            return f"\u274c Protection update failed: `{exc}`"
