from datetime import datetime, timezone
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_holding, fmt_price, fmt_pct, fmt_pnl
from telegram.ui import (
    compact_header, wib_now, pnl_emoji, progress_bar,
    ai_insight, build_message,
)
from scripts.position_status import is_open


def _parse_timestamp(ts: str) -> datetime | None:
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            clean = ts.split("+")[0].split("Z")[0]
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
    return None


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
            warning = "⚠️ Live sync failed (cached data may be stale)"
        else:
            warning = ""

        if not positions:
            return build_message(compact_header(), warning, "No open positions.")

        cards = [compact_header()]
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
                f"💰 Entry {entry_str} → 📍 {curr_str}\n"
                f"📈 PnL {pnl_str}  ·  📦 {qty:.6f} {base}  ·  🏦 {p.get('exchange', '?')}"
            )
            cards.append(card)

        return build_message(*cards)

    def _execute_paper(self, ctx, args: str = "") -> str:
        if ctx.services is not None:
            open_positions = ctx.services.metrics.open_positions()
        else:
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", [])
            open_positions = [p for p in pos_list if is_open(p.get("status"))]

        if not open_positions:
            return build_message(compact_header(), "No open positions.")

        cards = [compact_header()]

        for p in open_positions:
            symbol = p.get("symbol", "?")
            entry = p.get("entry_price", 0.0)
            current = p.get("current_price", 0.0)
            total_qty = p.get("quantity", 0.0)
            remaining = p.get("remaining_qty", total_qty)
            cost_basis = p.get("cost_basis", 0.0)

            # ``cost_basis`` in positions.json represents the *total* cost for
            # the original full quantity (including fees + slippage).  After a
            # partial TP sell, ``remaining_qty < quantity`` but ``cost_basis``
            # is still the full amount, so we must scale it down to the
            # remaining portion before computing PnL.
            #
            # Legacy entries without cost_basis: derive from entry price.
            if cost_basis <= 0 and entry > 0:
                cost_basis = entry * total_qty

            # Scale cost_basis to the remaining portion of the position.
            if total_qty > 0:
                cost_basis_remaining = cost_basis * (remaining / total_qty)
            else:
                cost_basis_remaining = cost_basis

            pnl = current * remaining - cost_basis_remaining
            # pnl_pct derived from the *actual* pnl vs cost paid — consistent
            # with the absolute pnl value shown next to it.
            # Guard: when cost_basis_remaining is 0 (legacy position with
            # missing cost data), fall back to entry-price calculation so
            # pnl_pct sign always matches the sign of pnl (no contradiction
            # like "-103 IDR (+28.04%)").
            if cost_basis_remaining > 0:
                pnl_pct = pnl / cost_basis_remaining * 100
            elif entry > 0 and remaining > 0:
                # Fallback: derive from entry price
                fallback_cost = entry * remaining
                pnl_pct = (current * remaining - fallback_cost) / fallback_cost * 100
            else:
                pnl_pct = 0.0

            emoji = pnl_emoji(pnl)

            holding_str = "N/A"
            entry_time_raw = p.get("opened_at", p.get("entry_time", ""))
            if entry_time_raw:
                dt = _parse_timestamp(entry_time_raw)
                if dt is not None:
                    hold_sec = (datetime.now(timezone.utc) - dt).total_seconds()
                    holding_str = fmt_holding(hold_sec)
            if holding_str == "N/A":
                holding_hours = p.get("holding_hours", 0)
                if holding_hours:
                    holding_str = fmt_holding(holding_hours * 3600)

            # Progress bar from entry to TP1 — only shown when there's an
            # actual TP1 to measure against, rather than faking a 50%
            # bar with no real meaning behind it.
            tp1 = p.get("tp1", 0.0)
            sl = p.get("current_stop") or p.get("stop_loss", 0.0)
            progress_line = ""
            if entry > 0 and tp1 > entry:
                pct_to_tp = max(0, min(100, (current - entry) / (tp1 - entry) * 100))
                bar = progress_bar(pct_to_tp, 100, 10)
                progress_line = f"Entry → TP1  {bar} {pct_to_tp:.0f}%\n"

            quote = symbol.split("/")[1] if "/" in symbol else "USDT"
            card = (
                f"{emoji} *{symbol}*  {fmt_pnl(pnl, quote)} ({pnl_pct:+.2f}%)\n"
                f"{progress_line}"
                f"🕒 Held {holding_str}"
            )
            cards.append(card)

        return build_message(*cards)
