from datetime import datetime, timezone
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_holding, fmt_price, fmt_pct, time_ago


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
        """LIVE positions — reconstructed straight from the exchange
        (balance + trade history), NOT from any local trade-plan
        simulation. See scripts/live_position_sync.py."""
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
            header = (
                f"\u26a0\ufe0f Live sync failed ({sync_error}) — showing "
                "last cached data, may be stale.\n\n"
            )
        else:
            header = ""

        if not positions:
            return "\U0001f7e2 *LIVE POSITIONS*\n\n" + header + "No open positions."

        lines = ["\U0001f7e2 *LIVE POSITIONS*", ""]
        if header:
            lines.append(header)
        for p in positions:
            symbol = p.get("symbol", "?")
            base = symbol.split("/")[0]
            qty = p.get("quantity", 0) or 0
            entry = p.get("entry_price")
            current = p.get("current_price")
            pnl_pct = p.get("pnl_pct")

            lines.append(f"*{symbol}*")
            lines.append(f"Amount:\n{qty:.6f} {base}")
            lines.append("")
            if entry is not None:
                lines.append(f"Entry:\n{entry:,.4f}")
            else:
                lines.append(
                    "Entry:\nUnknown (not enough trade history)\n"
                    "\u26a0\ufe0f _Not eligible for SL/TP protection until "
                    "this is resolved._",
                )
            lines.append("")
            lines.append(
                f"Current:\n{current:,.4f}" if current is not None
                else "Current:\nUnknown",
            )
            lines.append("")
            if pnl_pct is not None:
                emoji = "\U0001f7e2" if pnl_pct >= 0 else "\U0001f534"
                lines.append(f"PnL:\n{emoji} {pnl_pct:+.2f}%")
            else:
                lines.append("PnL:\nUnknown")
            lines.append("")
            lines.append(f"Exchange:\n{p.get('exchange', '?')}")
            lines.append("")
        return "\n".join(lines).strip()

    def _execute_paper(self, ctx, args: str = "") -> str:
        if ctx.services is not None:
            open_positions = ctx.services.metrics.open_positions()
        else:
            pos_data = ctx.read_json("positions.json")
            pos_list = pos_data.get("positions", [])
            open_positions = [p for p in pos_list if p.get("status") == "OPEN"]

        if not open_positions:
            return "No open positions."

        equity = 0.0
        pb = ctx.read_json("paper_balance.json")
        if pb:
            equity = pb.get("final_equity", 0.0)

        chunks = []
        for p in open_positions:
            symbol = p.get("symbol", "?")
            entry = p.get("entry_price", 0.0)
            current = p.get("current_price", 0.0)
            pnl = p.get("floating_pnl", 0)
            pnl_pct = p.get("floating_pnl_pct", 0)

            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

            roi_pct = ((current - entry) / entry * 100) if entry > 0 else 0.0
            dist_entry = ((current - entry) / entry * 100) if entry > 0 else 0.0

            sl = p.get("stop_loss", 0.0)
            tp1 = p.get("tp1", 0.0)
            tp2 = p.get("tp2", 0.0)
            tp3 = p.get("tp3", 0.0)

            # Remaining distance as % (positive means room before hit)
            rem_sl = ((current - sl) / current * 100) if current > 0 and sl > 0 else 0.0
            rem_tp1 = ((tp1 - current) / current * 100) if current > 0 and tp1 > 0 and not p.get("tp1_hit") else 0.0
            rem_tp2 = ((tp2 - current) / current * 100) if current > 0 and tp2 > 0 and not p.get("tp2_hit") else 0.0
            rem_tp3 = ((tp3 - current) / current * 100) if current > 0 and tp3 > 0 and not p.get("tp3_hit") else 0.0

            size = p.get("position_size_usdt", 0)
            exposure_pct = (size / equity * 100) if equity > 0 else 0.0

            entry_time_raw = p.get("entry_time", "")
            holding_str = "N/A"
            entry_time_str = "N/A"
            if entry_time_raw:
                try:
                    dt = datetime.fromisoformat(entry_time_raw.replace("Z", "+00:00"))
                    entry_time_str = time_ago(entry_time_raw)
                    hold_sec = (datetime.now(timezone.utc) - dt).total_seconds()
                    holding_str = fmt_holding(hold_sec)
                except (ValueError, TypeError):
                    pass
            if holding_str == "N/A":
                holding_hours = p.get("holding_hours", 0)
                if holding_hours:
                    holding_str = fmt_holding(holding_hours * 3600)

            # Risk/Reward
            rr = ((tp1 - entry) / (entry - sl)) if entry > 0 and sl > 0 and (entry - sl) > 0 else 0.0
            risk_pct = ((entry - sl) / entry * 100) if entry > 0 and sl > 0 else 0.0
            reward_pct = ((tp1 - entry) / entry * 100) if entry > 0 and tp1 > 0 else 0.0

            chunk = (
                f"{emoji} *{symbol}*\n"
                f"Entry: `{fmt_price(entry)}`  Curr: `{fmt_price(current)}`\n"
                f"Dist to Entry: `{dist_entry:+.2f}%`\n"
                f"Opened: `{entry_time_str}`  Holding: `{holding_str}`\n"
                f"Size: `{fmt_price(size)} USDT`  Exposure: `{exposure_pct:.1f}%`\n"
                f"PnL: `${pnl:+,.2f}` ({pnl_pct:+.2f}%)  ROI: `{roi_pct:+.2f}%`\n"
                f"SL: `{fmt_price(sl)}`  Remaining: `{rem_sl:.2f}%`\n"
                f"TP1: `{fmt_price(tp1)}`  Remaining: `{rem_tp1:.2f}%`\n"
                f"TP2: `{fmt_price(tp2)}`  Remaining: `{rem_tp2:.2f}%`\n"
                f"TP3: `{fmt_price(tp3)}`  Remaining: `{rem_tp3:.2f}%`\n"
                f"Risk: `{risk_pct:.2f}%`  Reward: `{reward_pct:.2f}%`  R/R: `{rr:.2f}`"
            )
            chunks.append(chunk)

        return "\n\n".join(chunks)
