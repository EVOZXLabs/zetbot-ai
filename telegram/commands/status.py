import os

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    compact_header, wib_now, progress_bar, exposure_bar,
    detail_block, build_message,
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
            # net_pnl (that number is already shown by /wallet's
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

        trading_label = "Paused" if paused else "Active"
        today_emoji = "🟢" if today_pnl >= 0 else "🔴"

        # One glance answers "is it working and how's it doing" — the
        # rest (exposure %, raw health score, pipeline state) is
        # secondary and lives in the collapsible breakdown below.
        return build_message(
            compact_header(),
            f"🟢 *ONLINE* — trading {trading_label}\n"
            f"Total Balance ${eq:,.2f} · Cash ${bal:,.2f}",
            f"Positions: {pos_label}\n"
            f"Today {today_emoji} ${today_pnl:+,.2f} · Next scan {next_scan_str}",
            detail_block(
                [
                    f"Pipeline    {pipeline_status}",
                    f"Uptime      {runtime}",
                    f"In trades   {exposure_pct:.1f}%",
                    f"Health      {health_score:.0f}/100",
                ],
                label="System",
            ),
            wib_now().replace("\n", ", "),
        )
