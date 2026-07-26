import threading

from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import compact_header, wib_now, build_message


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

        checks = [
            ("Bot", True, "Running", "Down"),
            ("Exchange", exchange_ok, "Connected", "Disconnected"),
            ("Telegram", tg_ok, "Connected", "Disconnected"),
            ("Scanner", scanner_ok, "Healthy", "Unhealthy"),
            ("Pipeline", pipeline_ok, "Healthy", "Unhealthy"),
        ]
        all_ok = all(ok for _, ok, _, _ in checks)

        # Headline answers the one question people actually ask ("is
        # anything broken?"); the per-component grid is the secondary
        # detail for anyone troubleshooting.
        lines = "\n".join(
            f"{'🟢' if ok else '🔴'} {label} — {good if ok else bad}"
            for label, ok, good, bad in checks
        )

        scanner_time = snapshot.get("scanner_time", "N/A")
        last_scan_line = ""
        if scanner_time != "N/A":
            try:
                from datetime import datetime, timezone, timedelta
                _wib = timezone(timedelta(hours=7))
                dt = datetime.fromisoformat(scanner_time.replace("Z", "+00:00"))
                wib_dt = dt.astimezone(_wib)
                last_scan_line = f"Last scan: {wib_dt.strftime('%d %b %Y %H:%M WIB')}"
            except (ValueError, AttributeError):
                last_scan_line = f"Last scan: {scanner_time}"

        return build_message(
            compact_header(),
            f"{'🟢' if all_ok else '🟡'} *SYSTEM HEALTH*\n"
            + ("Everything looks good." if all_ok else "Something needs attention."),
            lines,
            last_scan_line,
            wib_now().replace("\n", ", "),
        )
