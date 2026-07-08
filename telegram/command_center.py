import time
from typing import Any, Optional

from telegram.base_command import CommandMeta
from telegram.context import CommandContext
from telegram.formatter import bold, code, listify
from telegram.middleware import run_middleware
from telegram.registry import CommandRegistry


class CommandCenter:
    """New modular command center — replaces legacy dispatch."""

    def __init__(self, config: Any, logger: Any,
                 services: Any = None) -> None:
        self._config = config
        self._logger = logger
        self._services = services
        self._registry = CommandRegistry()
        self._registry.discover()

        from telegram import permissions  # noqa: PLC0415
        permissions.configure(str(config.telegram_chat_id))

    @property
    def registry(self) -> CommandRegistry:
        return self._registry

    # ------------------------------------------------------------------
    #  BotFather export
    # ------------------------------------------------------------------

    def export_botfather_commands(self) -> str:
        lines: list[str] = []
        for meta in self._registry.get_all_commands():
            if not meta.hidden:
                desc = meta.description.replace("\n", " ").strip()[:100]
                lines.append(f"{meta.name} - {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Help generation
    # ------------------------------------------------------------------

    def generate_help(self, user_is_admin: bool = True) -> str:
        parts = [bold("Available Commands\n")]
        for meta in self._registry.get_all_commands():
            if meta.hidden:
                continue
            if meta.permission == "admin" and not user_is_admin:
                continue
            usage = meta.usage or f"/{meta.name}"
            parts.append(f"{code(usage)} — {meta.description}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    #  Core dispatch
    # ------------------------------------------------------------------

    def dispatch(
        self,
        chat_id: str,
        message_id: int,
        update_id: int,
        text: str,
        **kwargs: Any,
    ) -> Optional[str]:
        """Parse, middleware, execute, return reply text."""

        command_name, _, args = text.partition(" ")
        command_name = command_name.lstrip("/").strip().lower()

        cls = self._registry.resolve(command_name)
        if cls is None:
            return None  # unknown command — silently ignore

        # Extract known fields from kwargs, pass remaining as extras
        known_fields = {
            k: kwargs.pop(k) for k in list(kwargs.keys())
            if hasattr(CommandContext, k)
        }

        ctx = CommandContext(
            config=self._config,
            logger=self._logger,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            raw_text=text,
            is_admin=True,
            **known_fields,
        )

        def execute() -> str:
            instance: Any = cls()
            return instance.execute(ctx, args)

        return run_middleware(
            ctx,
            command_name,
            execute,
            logger=self._logger,
        )

    # ------------------------------------------------------------------
    #  Legacy compatibility — wraps old polling loop
    # ------------------------------------------------------------------

    def handle_message(
        self,
        exchange: Any,
        chat_id: str,
        message_id: int,
        update_id: int,
        text: str,
        shutdown_event: Any,
        pid_file: str,
        start_time: float,
        health_monitor: Any,
    ) -> Optional[str]:
        return self.dispatch(
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            text=text,
            exchange=exchange,
            shutdown_event=shutdown_event,
            pid_file=pid_file,
            start_time=start_time,
            health_monitor=health_monitor,
            services=self._services,
        )
