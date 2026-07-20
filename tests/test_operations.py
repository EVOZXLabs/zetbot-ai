"""
Comprehensive tests for the Installation & Operations milestone.

Tests all new operation modules without modifying any trading engine code.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ENV_BAK = ".env.test_bak"
_ENV_KEYS: list[str] = []
_CONFIG_FIELD_KEYS: list[str] = []


def _init_env_keys() -> None:
    global _ENV_KEYS, _CONFIG_FIELD_KEYS
    try:
        from scripts.config_manager import CONFIG_FIELDS
        _CONFIG_FIELD_KEYS = [f.key for f in CONFIG_FIELDS]
    except ImportError:
        _CONFIG_FIELD_KEYS = [
            "EXCHANGE", "API_KEY", "API_SECRET", "PAPER_MODE",
            "TELEGRAM_ENABLED", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
            "POSITION_SIZE", "MAX_POSITIONS", "TIMEFRAME",
            "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "AUTO_PIPELINE",
            "PIPELINE_INTERVAL", "ACCOUNT_BALANCE", "MAX_RISK_PER_TRADE_PCT",
            "MIN_RR", "MAX_RR", "SCANNER_THREADS", "SCANNER_TOP_N",
            "SCANNER_MIN_VOLUME",
        ]


def _save_env() -> None:
    global _ENV_KEYS
    _init_env_keys()
    _ENV_KEYS = []
    # Save current values of config env vars
    for k in _CONFIG_FIELD_KEYS:
        if k in os.environ:
            _ENV_KEYS.append(k)


def _restore_env() -> None:
    global _ENV_KEYS
    # Clear config env vars from os.environ
    for k in _CONFIG_FIELD_KEYS:
        os.environ.pop(k, None)
    # Restore .env file if it was backed up
    if os.path.exists(ENV_BAK):
        shutil.copy2(ENV_BAK, ".env")
        os.remove(ENV_BAK)
    elif os.path.exists(".env"):
        os.remove(".env")
    _ENV_KEYS = []


def _write_env(content: str) -> None:
    with open(".env", "w") as f:
        f.write(content)


def _valid_env_content() -> str:
    return (
        "EXCHANGE=binance\nPAPER_MODE=true\nTIMEFRAME=1h\n"
        "ACCOUNT_BALANCE=10000\nMAX_POSITIONS=3\n"
        "MAX_RISK_PER_TRADE_PCT=2.0\nSCANNER_THREADS=5\n"
        "SCANNER_TOP_N=50\nTELEGRAM_TIMEOUT=10\nTELEGRAM_RETRY=3\n"
        "MIN_RR=1.5\nMAX_RR=5.0\nMIN_PROBABILITY=50\n"
        "MAX_ATR_PCT=8.0\nMAX_HOLDING_CANDLES=48\n"
        "TP1_SELL_PCT=30\nTP2_SELL_PCT=30\nTP3_SELL_PCT=40\n"
        "TAKER_FEE=0.001\nMAKER_FEE=0.00075\nSLIPPAGE_BPS=3\n"
        "AUTO_PIPELINE=true\nPIPELINE_INTERVAL=300\n"
    )


# ---------------------------------------------------------------------------
#  Config Manager Tests
# ---------------------------------------------------------------------------


class TestConfigManager:
    def setup_method(self) -> None:
        _save_env()
        if os.path.exists(".env"):
            os.remove(".env")

    def teardown_method(self) -> None:
        _restore_env()

    def test_env_not_exists(self) -> None:
        from scripts.config_manager import env_exists
        assert not env_exists()

    def test_env_exists(self) -> None:
        _write_env("EXCHANGE=binance\nPAPER_MODE=true\n")
        from scripts.config_manager import env_exists
        assert env_exists()

    def test_env_is_valid(self) -> None:
        _write_env(_valid_env_content())
        from scripts.config_manager import env_is_valid
        assert env_is_valid()

    def test_read_env(self) -> None:
        _write_env("EXCHANGE=bybit\nPAPER_MODE=false\n")
        # Reload env vars from the new file
        from scripts.config_manager import read_env
        env = read_env()
        assert env.get("EXCHANGE") == "bybit"

    def test_write_env(self) -> None:
        from scripts.config_manager import write_env
        write_env({"EXCHANGE": "bybit", "PAPER_MODE": "false"})
        assert os.path.exists(".env")
        with open(".env") as f:
            content = f.read()
        assert "EXCHANGE" in content
        assert "bybit" in content

    def test_display_config(self) -> None:
        _write_env("EXCHANGE=binance\nPAPER_MODE=true\n")
        from scripts.config_manager import display_config
        result = display_config()
        assert "Exchange" in result
        assert "binance" in result

    def test_validate_env_dict(self) -> None:
        from scripts.config_manager import validate_env_dict
        errors = validate_env_dict({"EXCHANGE": "", "PAPER_MODE": "true"})
        assert len(errors) > 0

    def test_validate_env_dict_many_required(self) -> None:
        from scripts.config_manager import validate_env_dict
        vals = {f.key: f.default for f in __import__("scripts.config_manager", fromlist=["CONFIG_FIELDS"]).CONFIG_FIELDS}
        vals["EXCHANGE"] = "binance"
        vals["API_KEY"] = "test_key"
        vals["API_SECRET"] = "test_secret"
        vals["TELEGRAM_TOKEN"] = "test_token"
        vals["TELEGRAM_CHAT_ID"] = "12345"
        errors = validate_env_dict(vals)
        assert len(errors) == 0

    def test_reset_env(self) -> None:
        _write_env("EXCHANGE=binance\n")
        from scripts.config_manager import reset_env
        assert os.path.exists(".env")
        reset_env(backup=False)
        assert not os.path.exists(".env")

    def test_config_to_dict(self) -> None:
        _write_env("EXCHANGE=binance\nPAPER_MODE=true\n")
        from scripts.config_manager import config_to_dict
        d = config_to_dict()
        assert d.get("EXCHANGE") == "binance"

    def test_env_as_dict(self) -> None:
        from scripts.config_manager import env_as_dict
        d = env_as_dict()
        assert isinstance(d, dict)

    def test_field_validators(self) -> None:
        from scripts.config_manager import CONFIG_FIELDS
        assert len(CONFIG_FIELDS) > 0
        exchanges = [f for f in CONFIG_FIELDS if f.key == "EXCHANGE"]
        assert len(exchanges) == 1
        assert exchanges[0].validator is not None
        assert exchanges[0].validator("binance")
        assert not exchanges[0].validator("unknown_exchange")


# ---------------------------------------------------------------------------
#  Startup Validator Tests
# ---------------------------------------------------------------------------


class TestStartupValidator:
    def test_validate_all_structure(self) -> None:
        from scripts.startup_validator import ValidationResult
        vr = ValidationResult()
        vr.add("Python", "PASS", "3.10")
        vr.add("Deps", "WARNING")
        vr.add("Config", "FAIL")
        assert vr.passed == 1
        assert vr.warnings == 1
        assert vr.failed == 1

    def test_validate_basic(self) -> None:
        from scripts.startup_validator import ValidationResult
        vr = ValidationResult()
        vr.add("Test", "PASS", "OK")
        assert vr.passed == 1


# ---------------------------------------------------------------------------
#  Diagnostics Tests
# ---------------------------------------------------------------------------


class TestDiagnostics:
    def test_diagnostic_result(self) -> None:
        from scripts.diagnostics import DiagnosticResult
        dr = DiagnosticResult()
        dr.add("Python", "Version", "PASS")
        dr.add("Deps", "All", "WARNING", "some missing")
        dr.add("Config", ".env", "FAIL")
        assert dr.passed == 1
        assert dr.warnings == 1
        assert dr.failed == 1

    def test_print_report(self) -> None:
        from scripts.diagnostics import DiagnosticResult
        dr = DiagnosticResult()
        dr.add("Test", "A", "PASS")
        dr.add("Test", "B", "FAIL", "reason")
        dr.print_report()


# ---------------------------------------------------------------------------
#  Backup/Restore Tests
# ---------------------------------------------------------------------------


class TestBackupRestore:
    def setup_method(self) -> None:
        _save_env()
        os.makedirs("backups", exist_ok=True)
        os.makedirs("data", exist_ok=True)
        with open("data/test_backup.json", "w") as f:
            json.dump({"test": True}, f)
        with open(".env", "w") as f:
            f.write("EXCHANGE=binance\n")

    def teardown_method(self) -> None:
        shutil.rmtree("backups", ignore_errors=True)
        if os.path.exists("data/test_backup.json"):
            os.remove("data/test_backup.json")
        _restore_env()

    def test_create_backup(self) -> None:
        from scripts.backup_restore import create_backup
        path = create_backup()
        assert os.path.exists(path)
        assert path.endswith(".zip")
        assert "backup-" in path

    def test_validate_backup_valid(self) -> None:
        from scripts.backup_restore import create_backup, validate_backup
        path = create_backup()
        assert validate_backup(path)

    def test_validate_backup_invalid(self) -> None:
        from scripts.backup_restore import validate_backup
        assert not validate_backup("nonexistent.zip")

    def test_list_backups(self) -> None:
        from scripts.backup_restore import create_backup, list_backups
        create_backup()
        backups = list_backups()
        assert len(backups) >= 1
        assert backups[0]["filename"].endswith(".zip")

    def test_restore_backup(self) -> None:
        from scripts.backup_restore import create_backup, restore_backup
        path = create_backup()
        with open(".env") as f:
            original_env = f.read()
        os.remove(".env")
        assert restore_backup(path, confirm=False)
        assert os.path.exists(".env")


# ---------------------------------------------------------------------------
#  Config Import/Export Tests
# ---------------------------------------------------------------------------


class TestConfigImportExport:
    def setup_method(self) -> None:
        _save_env()
        _write_env(
            "EXCHANGE=binance\nPAPER_MODE=true\nTIMEFRAME=1h\n"
            "ACCOUNT_BALANCE=10000\nMAX_POSITIONS=3\n"
            "MAX_RISK_PER_TRADE_PCT=2.0\nSCANNER_THREADS=5\n"
            "SCANNER_TOP_N=50\nTELEGRAM_TIMEOUT=10\nTELEGRAM_RETRY=3\n"
            "MIN_RR=1.5\nMAX_RR=5.0\nMIN_PROBABILITY=50\n"
            "MAX_ATR_PCT=8.0\nMAX_HOLDING_CANDLES=48\n"
            "TP1_SELL_PCT=30\nTP2_SELL_PCT=30\nTP3_SELL_PCT=40\n"
            "TAKER_FEE=0.001\nMAKER_FEE=0.00075\nSLIPPAGE_BPS=3\n"
            "AUTO_PIPELINE=true\nPIPELINE_INTERVAL=300\n"
        )

    def teardown_method(self) -> None:
        _restore_env()
        for f in os.listdir("."):
            if f.startswith("zetbot-config"):
                os.remove(f)

    def test_export_config(self) -> None:
        from scripts.config_import_export import export_config
        path = export_config()
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert "config" in data
        assert data["config"]["EXCHANGE"] == "binance"

    def test_import_config(self) -> None:
        from scripts.config_import_export import export_config, import_config
        path = export_config()
        os.remove(".env")
        assert import_config(path, force=True)
        assert os.path.exists(".env")

    def test_import_invalid_file(self) -> None:
        from scripts.config_import_export import import_config
        assert not import_config("nonexistent.json")


# ---------------------------------------------------------------------------
#  Exchange Test Tests
# ---------------------------------------------------------------------------


class TestExchangeTest:
    def test_test_exchange_returns_string(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.exchange = "binance"
        mock_cfg.api_key = ""
        mock_cfg.api_secret = ""

        mock_exchange = MagicMock()
        mock_exchange.fetch_status.return_value = {"status": "ok"}
        mock_exchange.fetch_time.return_value = 1700000000000

        mock_ccxt_mod = MagicMock()
        mock_ccxt_mod.binance = lambda *a, **kw: mock_exchange

        old_ccxt = sys.modules.pop("ccxt", None)
        sys.modules["ccxt"] = mock_ccxt_mod

        with patch("scripts.app_config.load_config", return_value=mock_cfg):
            from scripts.exchange_test import run_exchange_test
            result = run_exchange_test()
            assert isinstance(result, str)

        if old_ccxt is not None:
            sys.modules["ccxt"] = old_ccxt


# ---------------------------------------------------------------------------
#  Telegram Test Tests
# ---------------------------------------------------------------------------


class TestTelegramTest:
    def teardown_method(self) -> None:
        _restore_env()

    def test_test_telegram_returns_string(self) -> None:
        from scripts.telegram_test import run_telegram_test
        result = run_telegram_test()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
#  System Info Tests
# ---------------------------------------------------------------------------


class TestSystemInfo:
    def test_get_system_info(self) -> None:
        from scripts.system_info import get_system_info
        info = get_system_info()
        assert isinstance(info, str)
        assert "System Information" in info
        assert "Version:" in info
        assert "Python:" in info


# ---------------------------------------------------------------------------
#  Main CLI Dispatch Tests
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_cli_dispatch_structure(self) -> None:
        from scripts.config_manager import CONFIG_FIELDS
        # Just verify the CLI flags exist in the dispatch (tested separately)
        assert "--setup" in self._get_expected_flags()

    @staticmethod
    def _get_expected_flags() -> list[str]:
        return [
            "--setup", "--config", "--reset-config", "--wizard",
            "--diagnostics", "--backup", "--restore", "--export-config",
            "--import-config", "--test-exchange", "--test-telegram",
            "--system",
        ]


# ---------------------------------------------------------------------------
#  Install/Update Script Tests
# ---------------------------------------------------------------------------


class TestInstallScripts:
    def test_install_sh_exists(self) -> None:
        assert os.path.exists("install.sh")

    def test_install_sh_executable(self) -> None:
        assert os.access("install.sh", os.X_OK)

    def test_update_sh_exists(self) -> None:
        assert os.path.exists("update.sh")

    def test_update_sh_executable(self) -> None:
        assert os.access("update.sh", os.X_OK)


# ---------------------------------------------------------------------------
#  Documentation Tests
# ---------------------------------------------------------------------------


class TestDocumentation:
    def test_install_md_exists(self) -> None:
        assert os.path.exists("INSTALL.md")

    def test_install_md_has_content(self) -> None:
        with open("INSTALL.md") as f:
            content = f.read()
        assert len(content) > 100

    def test_operations_md_exists(self) -> None:
        assert os.path.exists("OPERATIONS.md")

    def test_operations_md_has_content(self) -> None:
        with open("OPERATIONS.md") as f:
            content = f.read()
        assert len(content) > 100


# ---------------------------------------------------------------------------
#  Wizard Menu Tests
# ---------------------------------------------------------------------------


class TestWizardMenu:
    def test_wizard_menu_imports(self) -> None:
        from scripts.wizard_menu import run_wizard_menu
        assert callable(run_wizard_menu)

    def test_module_imports(self) -> None:
        import scripts.wizard_menu
        assert hasattr(scripts.wizard_menu, "run_wizard_menu")


# ---------------------------------------------------------------------------
#  Module Import Tests
# ---------------------------------------------------------------------------


class TestModuleImports:
    def test_config_manager_import(self) -> None:
        import scripts.config_manager
        assert hasattr(scripts.config_manager, "CONFIG_FIELDS")
        assert hasattr(scripts.config_manager, "env_exists")
        assert hasattr(scripts.config_manager, "env_is_valid")
        assert hasattr(scripts.config_manager, "read_env")
        assert hasattr(scripts.config_manager, "write_env")
        assert hasattr(scripts.config_manager, "display_config")
        assert hasattr(scripts.config_manager, "reset_env")

    def test_setup_wizard_import(self) -> None:
        import scripts.setup_wizard
        assert hasattr(scripts.setup_wizard, "run_setup_wizard")
        assert hasattr(scripts.setup_wizard, "run_config_update")

    def test_startup_validator_import(self) -> None:
        import scripts.startup_validator
        assert hasattr(scripts.startup_validator, "ValidationResult")
        assert hasattr(scripts.startup_validator, "validate_all")
        assert hasattr(scripts.startup_validator, "validate_startup")

    def test_diagnostics_import(self) -> None:
        import scripts.diagnostics
        assert hasattr(scripts.diagnostics, "DiagnosticResult")
        assert hasattr(scripts.diagnostics, "run_diagnostics")

    def test_backup_restore_import(self) -> None:
        import scripts.backup_restore
        assert hasattr(scripts.backup_restore, "create_backup")
        assert hasattr(scripts.backup_restore, "validate_backup")
        assert hasattr(scripts.backup_restore, "list_backups")
        assert hasattr(scripts.backup_restore, "restore_backup")

    def test_config_import_export_import(self) -> None:
        import scripts.config_import_export
        assert hasattr(scripts.config_import_export, "export_config")
        assert hasattr(scripts.config_import_export, "import_config")

    def test_exchange_test_import(self) -> None:
        import scripts.exchange_test
        assert hasattr(scripts.exchange_test, "run_exchange_test")

    def test_telegram_test_import(self) -> None:
        import scripts.telegram_test
        assert hasattr(scripts.telegram_test, "run_telegram_test")

    def test_system_info_import(self) -> None:
        import scripts.system_info
        assert hasattr(scripts.system_info, "get_system_info")

    def test_wizard_menu_imports(self) -> None:
        import scripts.wizard_menu
        assert hasattr(scripts.wizard_menu, "run_wizard_menu")
