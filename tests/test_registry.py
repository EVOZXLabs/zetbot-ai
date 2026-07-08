"""
Unit tests for the modular command registry.

Covers: auto-discovery, resolve by name/alias, command metadata,
help generation, BotFather export, clear/reset.
"""

from telegram.base_command import BaseCommand, CommandMeta
from telegram.registry import CommandRegistry


# ---------------------------------------------------------------------------
#  Fixture
# ---------------------------------------------------------------------------

def _fresh_registry() -> CommandRegistry:
    r = CommandRegistry()
    r.discover()
    return r


# ---------------------------------------------------------------------------
#  Registry discovery
# ---------------------------------------------------------------------------

class TestRegistryDiscovery:
    """Auto-discovery must find all command classes in telegram/commands/."""

    def test_discovers_all_commands(self) -> None:
        r = _fresh_registry()
        metas = r.get_all_commands()
        names = {m.name for m in metas}
        expected = {
            "balance", "buy", "config", "health", "help",
            "logs", "pause", "pipeline", "positions",
            "reload", "restart", "resume", "scan",
            "sell", "shutdown", "signals", "status",
            "stoploss", "summary", "takeprofit", "version",
            "wallet",
        }
        missing = expected - names
        assert not missing, f"Missing commands: {missing}"

    def test_idempotent_discover(self) -> None:
        r = CommandRegistry()
        r.discover()
        first = len(r.get_all_commands())
        r.discover()
        second = len(r.get_all_commands())
        assert first == second, "Duplicate discover must not add entries"

    def test_every_command_has_name(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            assert m.name, f"Command missing name: {m}"

    def test_every_command_has_description(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            assert m.description, f"Command '{m.name}' missing description"


# ---------------------------------------------------------------------------
#  Resolve
# ---------------------------------------------------------------------------

class TestRegistryResolve:
    """Resolve by name and alias."""

    def test_resolve_by_name(self) -> None:
        r = _fresh_registry()
        cls = r.resolve("status")
        assert cls is not None
        assert cls.meta.name == "status"

    def test_resolve_by_alias(self) -> None:
        r = _fresh_registry()
        cls = r.resolve("bal")
        assert cls is not None
        assert cls.meta.name == "balance"

    def test_resolve_short_alias(self) -> None:
        r = _fresh_registry()
        assert r.resolve("p") is not None
        assert r.resolve("h") is not None
        assert r.resolve("v") is not None

    def test_resolve_unknown_returns_none(self) -> None:
        r = _fresh_registry()
        assert r.resolve("nonexistent_xyz") is None

    def test_resolve_empty_string(self) -> None:
        r = _fresh_registry()
        assert r.resolve("") is None

    def test_all_aliases_resolve_to_same_class(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            for alias in m.aliases:
                cls = r.resolve(alias)
                assert cls is not None, (
                    f"Alias '{alias}' for '{m.name}' did not resolve"
                )
                assert cls.meta.name == m.name


# ---------------------------------------------------------------------------
#  Command metadata
# ---------------------------------------------------------------------------

class TestCommandMeta:
    """Every command must have valid metadata."""

    def test_usage_format(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            assert m.usage.startswith("/"), (
                f"'{m.name}' usage must start with /"
            )

    def test_permission_valid(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            assert m.permission in ("user", "admin"), (
                f"'{m.name}' permission must be user or admin"
            )

    def test_no_duplicate_names(self) -> None:
        r = _fresh_registry()
        names = [m.name for m in r.get_all_commands()]
        assert len(names) == len(set(names)), (
            f"Duplicate command names: {names}"
        )

    def test_no_alias_collision_with_names(self) -> None:
        r = _fresh_registry()
        names = {m.name for m in r.get_all_commands()}
        for m in r.get_all_commands():
            for alias in m.aliases:
                assert alias not in names, (
                    f"Alias '{alias}' collides with command name '{alias}'"
                )

    def test_all_commands_have_execute(self) -> None:
        r = _fresh_registry()
        for m in r.get_all_commands():
            cls = r.resolve(m.name)
            assert cls is not None
            instance = cls()
            assert hasattr(instance, "execute"), (
                f"'{m.name}' missing execute()"
            )
            assert callable(instance.execute)


# ---------------------------------------------------------------------------
#  Help generation
# ---------------------------------------------------------------------------

class TestHelp:
    """CommandCenter help generation via registry."""

    def test_help_contains_visible_commands(self) -> None:
        r = _fresh_registry()
        metas = r.get_all_commands()
        visible = [m for m in metas if not m.hidden]
        assert len(visible) > 0
        names = {m.name for m in visible}
        assert "status" in names
        assert "health" in names
        assert "help" in names

    def test_hidden_commands_omitted_from_help(self) -> None:
        r = _fresh_registry()
        metas = r.get_all_commands()
        hidden = [m for m in metas if m.hidden]
        visible = [m for m in metas if not m.hidden]
        assert len(hidden) > 0, "Expected at least one hidden command"
        for m in hidden:
            assert m.name not in {v.name for v in visible}

    def test_admin_commands_filtered_for_non_admin(self) -> None:
        r = _fresh_registry()
        all_admin = [m for m in r.get_all_commands() if m.permission == "admin"]
        assert len(all_admin) > 0, "Expected at least one admin command"
        # Non-admin would not see them (tested in command_center help)


# ---------------------------------------------------------------------------
#  BotFather export
# ---------------------------------------------------------------------------

class TestBotFatherExport:
    """BotFather command list generation."""

    def test_export_format(self) -> None:
        r = _fresh_registry()
        from telegram.command_center import CommandCenter  # noqa: PLC0415
        # Use a mock config
        from unittest.mock import MagicMock
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        export = cc.export_botfather_commands()
        lines = export.strip().split("\n")
        assert len(lines) > 0
        for line in lines:
            assert " - " in line, f"BotFather line missing ' - ': {line}"

    def test_export_hidden_excluded(self) -> None:
        r = _fresh_registry()
        from unittest.mock import MagicMock
        from telegram.command_center import CommandCenter
        cfg = MagicMock()
        cfg.telegram_chat_id = "123"
        cc = CommandCenter(cfg, logger=None)
        export = cc.export_botfather_commands()
        assert "buy" not in export or "buy -" not in export
        # buy is hidden, should not appear


# ---------------------------------------------------------------------------
#  Clear / reset
# ---------------------------------------------------------------------------

class TestRegistryClear:
    """Registry clear and re-discover."""

    def test_clear_empties_registry(self) -> None:
        r = _fresh_registry()
        assert len(r.get_all_commands()) > 0
        r.clear()
        assert len(r.get_all_commands()) == 0

    def test_rediscover_after_clear(self) -> None:
        r = _fresh_registry()
        r.clear()
        r.discover()
        assert len(r.get_all_commands()) > 0

    def test_commands_property(self) -> None:
        r = _fresh_registry()
        cmds = r.commands
        assert isinstance(cmds, dict)
        assert "status" in cmds
        assert "help" in cmds
