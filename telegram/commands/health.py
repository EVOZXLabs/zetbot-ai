import threading

from telegram.base_command import BaseCommand, CommandMeta


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
        api_time = snapshot.get("api_time", "N/A")
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

        def _icon(status: bool, label_ok: str = "Healthy"):
            if status:
                return "\U0001f7e2", label_ok
            return "\U0001f534", "Critical"

        internet_icon, internet_status = _icon(internet_ok, "Connected" if internet_ok else "Disconnected")
        exchange_icon, exchange_status = _icon(exchange_ok, "Connected")

        scanner_age = snapshot.get("scanner_age", float("inf"))
        if scanner_age == float("inf"):
            scanner_icon, scanner_status = "\U0001f534", "No Data"
        elif scanner_age < 7200:
            scanner_icon, scanner_status = "\U0001f7e2", "Healthy"
        elif scanner_age < 86400:
            scanner_icon, scanner_status = "\U0001f7e1", "Stale"
        else:
            scanner_icon, scanner_status = "\U0001f534", "Critical"

        has_creds = bool(ctx.config.telegram_token and ctx.config.telegram_chat_id)
        tg_alive = False
        if has_creds:
            for t in threading.enumerate():
                if t.name == "TelegramCmd" and t.is_alive():
                    tg_alive = True
                    break
        if has_creds and tg_alive:
            telegram_icon, telegram_status = "\U0001f7e2", "Healthy"
        elif has_creds:
            telegram_icon, telegram_status = "\U0001f534", "Not Running"
        else:
            telegram_icon, telegram_status = "\u26aa", "Disabled"

        cpu_icon = "\U0001f7e2" if cpu_pct < 50 else ("\U0001f7e1" if cpu_pct < 80 else "\U0001f534")
        cpu_label = "Healthy" if cpu_pct < 50 else ("Warning" if cpu_pct < 80 else "Critical")
        mem_icon = "\U0001f7e2" if rss_mb < 100 else ("\U0001f7e1" if rss_mb < 250 else "\U0001f534")
        mem_label = "Healthy" if rss_mb < 100 else ("Warning" if rss_mb < 250 else "Critical")
        thr_icon = "\U0001f7e2" if thread_count < 15 else ("\U0001f7e1" if thread_count < 30 else "\U0001f534")
        thr_label = "Healthy" if thread_count < 15 else ("Warning" if thread_count < 30 else "Critical")

        score = 100
        if not internet_ok:
            score -= 20
        if not exchange_ok:
            score -= 20
        if scanner_status == "Critical":
            score -= 15
        elif scanner_status == "Stale":
            score -= 5
        if telegram_status == "Not Running":
            score -= 15
        if cpu_label == "Critical":
            score -= 10
        elif cpu_label == "Warning":
            score -= 5
        if mem_label == "Critical":
            score -= 10
        elif mem_label == "Warning":
            score -= 5
        if thr_label == "Critical":
            score -= 5
        elif thr_label == "Warning":
            score -= 3
        score = max(0, min(100, score))
        score_icon = "\U0001f7e2" if score >= 80 else ("\U0001f7e1" if score >= 50 else "\U0001f534")

        return (
            f"{score_icon} *ZetBot {ver} Health*\n\n"
            f"*Score:* `{score}/100`\n\n"
            f"*System*\n"
            f"Uptime:     `{uptime_str}`\n"
            f"Mode:       `{'PAPER' if paper_mode else 'LIVE'}`\n"
            f"Trading:    `{'PAUSED \u23f8\ufe0f' if paused else 'ACTIVE'}`\n\n"
            f"*Resources*\n"
            f"CPU:        `{cpu_pct:.1f}%`  {cpu_icon} {cpu_label}\n"
            f"Memory:     `{rss_mb:.1f}MB`  {mem_icon} {mem_label}\n"
            f"Threads:    `{thread_count}`  {thr_icon} {thr_label}\n\n"
            f"*Components*\n"
            f"Internet:   {internet_icon} {internet_status}\n"
            f"Exchange:   {exchange_icon} {exchange_status}\n"
            f"Scanner:    {scanner_icon} {scanner_status}\n"
            f"Telegram:   {telegram_icon} {telegram_status}\n\n"
            f"*Account*\n"
            f"Equity:     `${equity:,.2f}`\n"
            f"Cash:       `${balance:,.2f}`\n"
            f"Net PnL:    `${net_pnl:+,.2f}`\n"
            f"Win Rate:   `{win_rate:.1f}%`\n\n"
            f"*Positions*\n"
            f"Open:       `{open_positions}`\n"
            f"Total:      `{total_trades}`\n\n"
            f"*Timestamps*\n"
            f"Last Scan:  `{scanner_time}`\n"
            f"Last Trade: `{api_time}`"
        )
