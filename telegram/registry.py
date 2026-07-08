import importlib
import inspect
import pkgutil
from typing import Any

from telegram.base_command import BaseCommand, CommandMeta


class CommandRegistry:
    """Auto-discovers command classes and resolves by name/alias."""

    def __init__(self) -> None:
        self._name_map: dict[str, type[BaseCommand]] = {}
        self._loaded = False

    def discover(self, package_path: str = "telegram.commands") -> None:
        if self._loaded:
            return

        import telegram.commands as pkg  # noqa: PLC0415

        for mod_info in pkgutil.iter_modules(pkg.__path__):
            if mod_info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{package_path}.{mod_info.name}")
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseCommand)
                    and obj is not BaseCommand
                    and hasattr(obj, "meta")
                ):
                    self._register(obj)

        self._loaded = True

    def _register(self, cls: type[BaseCommand]) -> None:
        meta: CommandMeta = cls.meta
        self._name_map[meta.name] = cls
        for alias in meta.aliases:
            self._name_map[alias] = cls

    def resolve(self, name: str) -> type[BaseCommand] | None:
        return self._name_map.get(name)

    def get_all_commands(self) -> list[CommandMeta]:
        seen: set[int] = set()
        result: list[CommandMeta] = []
        for cls in self._name_map.values():
            uid = id(cls)
            if uid not in seen:
                seen.add(uid)
                result.append(cls.meta)
        return sorted(result, key=lambda m: m.name)

    @property
    def commands(self) -> dict[str, type[BaseCommand]]:
        return dict(self._name_map)

    def clear(self) -> None:
        self._name_map.clear()
        self._loaded = False
