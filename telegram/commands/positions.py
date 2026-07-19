from datetime import datetime, timezone
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_holding, fmt_price, fmt_pct
from telegram.ui import (
    header, SEPARATOR, wib_now, pnl_emoji, progress_bar,
    ai_insight, build_message,
)
from scripts.position_status import is_open


class PositionsCommand(BaseCommand):
    meta = CommandMeta(
        name="positions",
        aliases=["pos", "position"],
        description="Show all open positions with details",
        usage="/positions",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        if (
            ctx.services is not None
            and getattr(ctx.services, "order", None) is not None
            and ctx.services.order.mode == "LIVE"
        ):
            return self._execute_live(ctx)
        return self._execute_paper(ctx)

    def _execute_live(self, ctx) -> str:
        """LIVE positions — reconstructed straight from the exchange."""
        from scripts.live_position_sync import (  # noqa: PLC0415
            LivePositionSync,
            merge_live_positions,
        )
        from scripts.exchange_providers import ExchangeAuthError  # noqa: PLC0415

        exchange = ctx.services.exchange
        quote = getattr(ctx.services.config, "quote_currency", "USDT") or "USDT"

        try:
            syncer = LivePositionSync(exchange, quote_currency=quote)
            fresh = syncer.sync_all_positions()
            merge_live_positions(fresh, synced_symbols=[p["symbol"] for p in fresh])
            positions = fresh
            sync_error = None
        except ExchangeAuthError as exc:
            positions = None
            sync_error = str(exc)
        except Exception as exc:
            positions = None
            sync_error = f"unexpected error: {exc}"

        if positions is None:
            from scripts.live_position_sync import load_live_positions  # noqa: PLC0415
            cached = load_live_positions()
            positions = list(cached.values())
            warning = f"⚠️ Live sync failed ({sync_error}) — cached data may be stale.\n\n"
        else:
            warning = ""

        if not positions:
            return f"{header()}\n\n{warning}No open positions."

        cards = [header()]
        if warning:
            cards.append(warning)

        for p in positions:
            symbol = p.get("symbol", "?")
            base = symbol.split("/")[0]
            qty = p.get("quantity", 0) or 0
            entry = p.get("entry_price")
            current = p.get("current_price")
            pnl_pct = p.get("pnl_pct")

            emoji = pnl_emoji(pnl_pct) if pnl_pct is not None else "⚪"
            pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "N/A"
            entry_str = fmt_price(entry) if entry else "Unknown"
            curr_str = fmt_price(current) if current else "Unknown"

            card = (
                f"{emoji} *{symbol}*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 Entry {entry_str}\n"
                f"📍 Current {curr_str}\n"
                f"📈 PnL {pnl_str}\n"
                f"📦 {qty:.6f} {base}\n"
                f"🏦 {p.get('exchange', '?')}"
            )
            cards.append(card)

        return "\n\n".join(cards)

    def _execute_paper(self, ctx, args: str = "") -> str:
        if ctx.services is not None:
            open_positions = ctx.services.metrics.open_positions()
        else:
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", [])
            open_positions = [p for p in pos_list if is_open(p.get("status"))]

        if not open_positions:
            return f"{header()}\n\nNo open positions."

        cards = [header()]

        for p in open_positions:
            symbol = p.get("symbol", "?")
            entry = p.get("entry_price", 0.0)
            current = p.get("current_price", 0.0)
            pnl = p.get("floating_pnl", 0)
            pnl_pct = p.get("floating_pnl_pct", 0)

            emoji = pnl_emoji(pnl)

            holding_str = "N/A"
            entry_time_raw = p.get("opened_at", p.get("entry_time", ""))
            if entry_time_raw:
                try:
                    dt = datetime.fromisoformat(entry_time_raw.replace("Z", "+00:00"))
                    hold_sec = (datetime.now(timezone.utc) - dt).total_seconds()
                    holding_str = fmt_holding(hold_sec)
                except (ValueError, TypeError):
                    pass
            if holding_str == "N/A":
                holding_hours = p.get("holding_hours", 0)
                if holding_hours:
                    holding_str = fmt_holding(holding_hours * 3600)

            # Progress bar from entry to TP1
            tp1 = p.get("tp1", 0.0)
            sl = p.get("current_stop") or p.get("stop_loss", 0.0)
            if entry > 0 and tp1 > 0:
                pct_to_tp = max(0, min(100, (current - entry) / (tp1 - entry) * 100))
                bar = progress_bar(pct_to_tp, 100, 10)
            else:
                bar = progress_bar(50, 100, 10)

            card = (
                f"{emoji} *{symbol}*  ${pnl:+,.2f}\n"
                f"{bar}\n"
                f"🕒 Held {holding_str}"
            )
            cards.append(card)

        return "\n━━━━━━━━━━━━━━━━━━\n\n".join(cards)
