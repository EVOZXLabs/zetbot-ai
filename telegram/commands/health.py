import threading

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import (
    header, SEPARATOR, wib_now, status_dot, build_message,
)


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

        internet_ok = snapshot.get("internet_ok", False)
        exchange_ok = snapshot.get("exchange_ok", False)

        scanner_status_raw = snapshot.get("scanner_status", "no_data")
        scanner_ok = scanner_status_raw in ("healthy",)

        # Telegram thread check
        has_creds = bool(ctx.config.telegram_token and ctx.config.telegram_chat_id)
        tg_alive = False
        if has_creds:
            for t in threading.enumerate():
                if t.name == "TelegramCmd" and t.is_alive():
                    tg_alive = True
                    break
        tg_ok = has_creds and tg_alive

        pipeline_ok = True
        if ctx.services is not None and ctx.services.scheduler is not None:
            s_status = ctx.services.scheduler.status
            pipeline_ok = s_status not in ("stopped",) and not s_status.startswith("failed")

        scanner_time = snapshot.get("scanner_time", "N/A")
        last_scan_str = ""
        if scanner_time != "N/A":
            from telegram.formatter import time_ago
            last_scan_str = f"\n\nLast Scan\n{time_ago(scanner_time)}"

        return build_message(
            header(),
            f"❤️ *SYSTEM HEALTH*\n{SEPARATOR}",
            f"{status_dot(internet_ok, 'Bot')}  🟢 Running",
            f"{status_dot(exchange_ok, 'Exchange')}  {'🟢 Connected' if exchange_ok else '🔴 Disconnected'}",
            f"{status_dot(tg_ok, 'Telegram')}  {'🟢 Connected' if tg_ok else '🔴 Disconnected'}",
            f"{status_dot(scanner_ok, 'Scanner')}  {'🟢 Healthy' if scanner_ok else '🔴 Unhealthy'}",
            f"{status_dot(pipeline_ok, 'Pipeline')}  {'🟢 Healthy' if pipeline_ok else '🔴 Unhealthy'}",
            f"{SEPARATOR}{last_scan_str}",
        )
