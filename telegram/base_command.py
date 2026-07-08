from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommandMeta:
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    permission: str = "user"  # user | admin
    hidden: bool = False
    examples: list[str] = field(default_factory=list)


class BaseCommand(ABC):
    """Every command inherits from this and defines meta + execute()."""

    meta: CommandMeta

    @abstractmethod
    def execute(self, ctx: Any, args: str) -> str:
        ...
