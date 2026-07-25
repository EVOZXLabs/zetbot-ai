import os

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    header, SEPARATOR, wib_now, wib_short, progress_bar,
    exposure_bar, build_message,
)
from scripts.position_status import is_open

PAUSE_FILE = "data/.paused"


class StatusCommand(BaseCommand):
    meta = CommandMeta(
        name="status",
        aliases=["stats"],
        description="Bot status, runtime, balance, positions overview",
        usage="/status",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        m = ctx.services.metrics if ctx.services else None

        # Uptime
        uptime_sec = 0
        if ctx.services is not None and ctx.services.health is not None:
            uptime_sec = ctx.services.health.uptime_sec
        hours, rem = divmod(int(uptime_sec), 3600)
        minutes, seconds = divmod(rem, 60)
        runtime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        # Financial data (single source: MetricsManager)
        if m is not None:
            a = m.account()
            bal = a.balance
            eq = a.equity
            net_pnl = a.net_pnl
            open_pos = a.open_positions
            win_rate = a.win_rate
            exposure_pct = a.exposure_pct
            # "Today" must be today's realized PnL, not the all-time
            # net_pnl (that number is already shown by /balance's
            # "all-time" line — reusing it here made the two commands
            # display identical figures under different labels).
            today_pnl = m.today_summary().get("pnl", 0.0)

            # Build position symbols string
            open_list = m.open_positions()
            if open_list:
                symbols = ", ".join(
                    p.get("symbol", "?") for p in open_list
                )
                pos_label = f"{open_pos}: {symbols}"
            else:
                pos_label = "None"
        else:
            pb = ctx.read_json("paper_balance.json")
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            open_pos = 0
            win_rate = pb.get("win_rate", 0.0)
            exposure_pct = 0.0
            pos_label = "None"
            # No MetricsManager available in this fallback path — best
            # approximation is the all-time figure (same limitation as
            # before this fix).
            today_pnl = net_pnl

        paused = os.path.exists(PAUSE_FILE)

        # Scheduler & pipeline status
        pipeline_status = "Stopped"
        next_scan_str = "N/A"
        health_score = 0

        if ctx.services is not None:
            sched = ctx.services.scheduler
            if sched is not None:
                s_status = sched.status
                if s_status == "stopped":
                    pipeline_status = "Stopped"
                elif s_status == "running":
                    pipeline_status = "Running..."
                elif s_status.startswith("failed"):
                    pipeline_status = s_status
                else:
                    pipeline_status = "Idle"

                if sched.next_run:
                    import datetime
                    next_ts = datetime.datetime.fromtimestamp(
                        sched.next_run, tz=datetime.timezone.utc
                    )
                    from telegram.ui import _WIB
                    next_ts_local = next_ts.astimezone(_WIB)
                    next_scan_str = next_ts_local.strftime("%H:%M WIB")

            if ctx.services.health is not None:
                try:
                    snap = ctx.services.health.snapshot()
                    hs = snap.get("health_score")
                    if hs is not None:
                        health_score = hs
                except Exception:
                    pass

        trading_label = "PAUSED" if paused else "ACTIVE"
        today_emoji = "🟢" if today_pnl >= 0 else "🔴"

        return build_message(
            header(),
            f"🟢 *ONLINE*\n"
            f"💰 Equity\n${eq:,.2f}\n\n"
            f"💵 Cash\n${bal:,.2f}\n\n"
            f"📂 Positions\n{pos_label}",
            f"{SEPARATOR}\n"
            f"📈 Today\n{today_emoji} ${today_pnl:+,.2f}\n\n"
            f"⏰ Next Scan\n{next_scan_str}",
            f"{SEPARATOR}\n"
            f"⚠️ Exposure\n{exposure_bar(exposure_pct)}\n\n"
            f"❤️ Health\n{progress_bar(health_score, 100, 10)} {health_score:.0f}",
            f"🕐 {wib_now()}",
        )
