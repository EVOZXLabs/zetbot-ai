import datetime
import os

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import fmt_compact_number, time_ago

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
        else:
            pb = ctx.read_json("paper_balance.json")
            bal = pb.get("final_balance", 0.0)
            eq = pb.get("final_equity", 0.0)
            net_pnl = pb.get("net_pnl", 0.0)
            open_pos = 0
            win_rate = pb.get("win_rate", 0.0)

        paused = os.path.exists(PAUSE_FILE)

        # Scheduler & pipeline status
        scheduler_status = "N/A"
        pipeline_status = "N/A"
        last_scan_str = "N/A"
        next_scan_str = "N/A"
        health_score = "N/A"

        if ctx.services is not None:
            sched = ctx.services.scheduler
            if sched is not None:
                s_status = sched.status
                if s_status == "stopped":
                    scheduler_status = "Stopped"
                    pipeline_status = "Stopped"
                elif s_status == "running":
                    scheduler_status = "Active"
                    pipeline_status = "Running..."
                elif s_status.startswith("failed"):
                    scheduler_status = "Active"
                    pipeline_status = s_status
                else:
                    scheduler_status = "Active"
                    pipeline_status = "Idle"

                if sched.next_run:
                    next_ts = datetime.datetime.fromtimestamp(
                        sched.next_run, tz=datetime.timezone.utc
                    ).strftime("%H:%M:%S UTC")
                    next_scan_str = next_ts
                if sched.last_start:
                    last_scan_str = time_ago(
                        datetime.datetime.fromtimestamp(
                            sched.last_start, tz=datetime.timezone.utc
                        ).isoformat()
                    )

            # Health score from health monitor
            if ctx.services.health is not None:
                try:
                    snap = ctx.services.health.snapshot()
                    hs = snap.get("health_score")
                    if hs is not None:
                        health_score = f"{hs:.0f}"
                    scanner_time_raw = snap.get("scanner_time", "N/A")
                    if scanner_time_raw != "N/A":
                        last_scan_str = time_ago(scanner_time_raw)
                except Exception:
                    pass

        return (
            f"\U0001f916 *Bot Status*\n"
            f"Mode: `{'PAPER' if ctx.config.paper_mode else 'LIVE'}`  "
            f"Exchange: `{ctx.config.exchange}`\n"
            f"Runtime: `{runtime}`  "
            f"Trading: `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`\n"
            f"\n"
            f"*Pipeline*\n"
            f"Scheduler: `{scheduler_status}`  "
            f"Pipeline: `{pipeline_status}`\n"
            f"Last Scan: `{last_scan_str}`  "
            f"Next Scan: `{next_scan_str}`\n"
            f"\n"
            f"*Account*\n"
            f"Open Positions: `{open_pos}`\n"
            f"Cash: `{fmt_compact_number(bal)}`  "
            f"Equity: `{fmt_compact_number(eq)}`\n"
            f"Net PnL: `{net_pnl:+,.2f}`  "
            f"Win Rate: `{win_rate:.1f}%`\n"
            f"Health: `{health_score}`"
        )
