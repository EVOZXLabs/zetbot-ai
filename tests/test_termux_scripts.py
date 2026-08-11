"""Static sanity tests for the Termux operational scripts.

These are offline, deterministic checks: the scripts are validated for bash
syntax and for the operational features they must contain. Real behaviour is
verified manually (see OPERATIONS.md → "Menjalankan di Termux").
"""

import os
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

START = os.path.join(ROOT, "scripts", "termux-start.sh")
BOOT = os.path.join(ROOT, "scripts", "termux-boot", "zetbot-start.sh")


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and bool(os.stat(path).st_mode & stat.S_IXUSR)


def _bash_syntax_ok(path: str) -> None:
    result = subprocess.run(
        ["bash", "-n", path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"bash -n failed for {path}:\n{result.stderr}"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class TestTermuxStart:

    def test_exists_and_executable(self):
        assert _is_executable(START)

    def test_bash_syntax(self):
        _bash_syntax_ok(START)

    def test_wake_lock_install_instructions(self):
        text = _read(START)
        assert "termux-wake-lock" in text
        assert "pkg install termux-api" in text
        assert "F-Droid" in text

    def test_stale_flag_cleanup_only_when_old(self):
        text = _read(START)
        assert "STALE_FLAG_MINUTES" in text
        assert "clean_stale_flag" in text

    def test_dedicated_tmux_sessions(self):
        text = _read(START)
        assert "zetbot-bot" in text
        assert "zetbot-watchdog" in text
        assert "has-session" in text

    def test_verify_checks_notifier_exit_code(self):
        text = _read(START)
        assert "--verify" in text
        assert "NOTIFIER_OK" in text
        assert "kill -9" in text

    def test_verify_rejects_disabled_notifier(self):
        text = _read(START)
        assert "NOTIFIER_DISABLED" in text
        assert "TELEGRAM_ENABLED=true" in text


class TestTermuxBoot:

    def test_exists_and_executable(self):
        assert _is_executable(BOOT)

    def test_bash_syntax(self):
        _bash_syntax_ok(BOOT)

    def test_delegates_to_termux_start(self):
        text = _read(BOOT)
        assert "termux-start.sh" in text
        assert "$HOME/.termux/boot" in text or "~/.termux/boot" in text

    def test_termux_shebang(self):
        with open(BOOT) as f:
            assert f.readline().strip().startswith("#!/data/data/com.termux")


class TestBootReadme:

    def test_readme_exists(self):
        path = os.path.join(ROOT, "scripts", "termux-boot", "README.md")
        assert os.path.isfile(path)
        text = _read(path)
        assert "Termux:Boot" in text
        assert "F-Droid" in text
