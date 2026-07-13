import threading

from telegram.base_command import BaseCommand, CommandMeta
from telegram.formatter import time_ago


class HealthCommand(BaseCommand):
    meta = CommandMeta(
        name="health",
        aliases=["h", "status-detail"],
        description="System health and component status overview",
        usage="/health",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        snapshot: dict = {}
        if ctx.health_monitor:
            try:
                snapshot = ctx.health_monitor.force_refresh()
            except Exception:
                pass

        ver = snapshot.get("version", "?")
        uptime_sec = snapshot.get("uptime_sec", 0)
        rss_kb = snapshot.get("rss_kb", 0)
        thread_count = snapshot.get("thread_count", 0)
        process_cpu_sec = snapshot.get("process_cpu_sec", 0)
        internet_ok = snapshot.get("internet_ok", False)
        exchange_ok = snapshot.get("exchange_ok", False)
        scanner_time = snapshot.get("scanner_time", "N/A")
        scanner_timeout = snapshot.get("scanner_timeout", 7200)
        scanner_age = snapshot.get("scanner_age", -1)
        api_time = snapshot.get("api_time", "N/A")
        api_age = snapshot.get("api_age", -1)
        balance = snapshot.get("balance", 0.0)
        equity = snapshot.get("equity", 0.0)
        net_pnl = snapshot.get("net_pnl", 0.0)
        open_positions = snapshot.get("open_positions", 0)
        total_trades = snapshot.get("total_trades", 0)
        win_rate = snapshot.get("win_rate", 0.0)
        paused = snapshot.get("paused", False)
        paper_mode = snapshot.get("paper_mode", True)

        hours, rem = divmod(int(uptime_sec), 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        cpu_pct = (process_cpu_sec / uptime_sec * 100) if uptime_sec > 0 else 0.0
        rss_mb = rss_kb / 1024.0

        mem_total_mb = 0.0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            mem_total_mb = int(parts[1]) / 1024.0
                        break
        except (OSError, ValueError):
            pass
        mem_pct = (rss_mb / mem_total_mb * 100) if mem_total_mb > 0 else 0.0

        def _icon(ok: bool) -> str:
            return "\U0001f7e2" if ok else "\U0001f534"

        internet_icon = _icon(internet_ok)
        exchange_icon = _icon(exchange_ok)

        scanner_status_raw = snapshot.get("scanner_status", "no_data")
        if scanner_status_raw == "healthy":
            scanner_icon = "\U0001f7e2"
            scanner_label = "Fresh"
        elif scanner_status_raw == "stale":
            scanner_icon = "\U0001f7e1"
            scanner_label = "Stale"
        elif scanner_status_raw == "critical":
            scanner_icon = "\U0001f534"
            scanner_label = "Critical"
        else:
            scanner_icon = "\U0001f534"
            scanner_label = "No Data"

        has_creds = bool(ctx.config.telegram_token and ctx.config.telegram_chat_id)
        tg_alive = False
        if has_creds:
            for t in threading.enumerate():
                if t.name == "TelegramCmd" and t.is_alive():
                    tg_alive = True
                    break
        if has_creds and tg_alive:
            telegram_icon, telegram_label = "\U0001f7e2", "Healthy"
        elif has_creds:
            telegram_icon, telegram_label = "\U0001f534", "Not Running"
        else:
            telegram_icon, telegram_label = "\u26aa", "Disabled"

        cpu_icon = "\U0001f7e2" if cpu_pct < 50 else ("\U0001f7e1" if cpu_pct < 80 else "\U0001f534")
        cpu_label = "Healthy" if cpu_pct < 50 else ("Warning" if cpu_pct < 80 else "Critical")

        # Memory trend indicator (relative to total)
        if mem_total_mb > 0:
            if mem_pct < 30:
                mem_icon, mem_label, mem_trend = "\U0001f7e2", "Healthy", "\U0001f4c9"
            elif mem_pct < 60:
                mem_icon, mem_label, mem_trend = "\U0001f7e1", "Warning", "\U0001f4c8"
            else:
                mem_icon, mem_label, mem_trend = "\U0001f534", "Critical", "\U0001f4c8"
        else:
            mem_icon, mem_label, mem_trend = "\u26aa", "Unknown", ""

        score = 100
        if not internet_ok: score -= 20
        if not exchange_ok: score -= 20
        if scanner_label == "Critical": score -= 15
        elif scanner_label == "Stale": score -= 5
        if telegram_label == "Not Running": score -= 15
        if cpu_label == "Critical": score -= 10
        elif cpu_label == "Warning": score -= 5
        if mem_label == "Critical": score -= 10
        elif mem_label == "Warning": score -= 5
        score = max(0, min(100, score))
        score_icon = "\U0001f7e2" if score >= 80 else ("\U0001f7e1" if score >= 50 else "\U0001f534")

        # Relative timestamps
        last_scan_rel = time_ago(scanner_time) if scanner_time != "N/A" else "N/A"
        last_trade_rel = time_ago(api_time) if api_time != "N/A" else "N/A"

        return (
            f"{score_icon} *ZetBot {ver} Health*\n"
            f"*Score:* `{score}/100`\n"
            f"\n"
            f"*Components*\n"
            f"Scanner: {scanner_icon} `{last_scan_rel}` {scanner_label}\n"
            f"Last Trade: {exchange_icon} `{last_trade_rel}`\n"
            f"Telegram: {telegram_icon} {telegram_label}\n"
            f"Exchange: {exchange_icon} Connected\n"
            f"Internet: {internet_icon} Connected\n"
            f"\n"
            f"*System*\n"
            f"Uptime: `{uptime_str}`  Mode: `{'PAPER' if paper_mode else 'LIVE'}`\n"
            f"Trading: `{'PAUSED' if paused else 'ACTIVE'}`\n"
            f"\n"
            f"*Resources*\n"
            f"Memory: `{rss_mb:.1f}MB` ({mem_pct:.1f}%)  {mem_icon} {mem_label} {mem_trend}\n"
            f"CPU:    `{cpu_pct:.1f}%`  {cpu_icon} {cpu_label}\n"
            f"Threads: `{thread_count}`\n"
            f"\n"
            f"*Positions*\n"
            f"Open: `{open_positions}`\n"
            f"Total: `{open_positions}`\n"
            f"\n"
            f"*Timestamps*\n"
            f"Last Scan: `{last_scan_rel}`\n"
            f"Last Trade: `{last_trade_rel}`\n"
            f"\n"
            f"*Account*\n"
            f"Equity: `${equity:,.2f}`  Cash: `${balance:,.2f}`\n"
            f"Net PnL: `${net_pnl:+,.2f}`  Win Rate: `{win_rate:.1f}%`"
        )
