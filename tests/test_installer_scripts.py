"""Tests for the one-click Termux installer (install.sh / run.sh / update.sh / uninstall.sh).

Two layers:

* Static checks (offline, deterministic) — files exist, are executable,
  pass ``bash -n``, and contain the required behaviours (Termux detection,
  ``pkg update && pkg upgrade``, the six system packages, virtualenv,
  requirements, ``.env`` guard, data folders, self-check, PASS/FAIL output).

* Behavioural sandbox tests — the scripts are executed inside a tmp project
  dir with FAKE ``pkg`` / ``apt-get`` / ``python`` / ``git`` binaries on the
  PATH, so the whole flow is exercised end-to-end with no network and no
  mutation of the real system.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = ["install.sh", "run.sh", "update.sh", "uninstall.sh"]


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _script_path(name: str) -> Path:
    return ROOT / name


def _read(name: str) -> str:
    return _script_path(name).read_text(encoding="utf-8")


def _is_executable(path: Path) -> bool:
    return path.is_file() and bool(path.stat().st_mode & stat.S_IXUSR)


def _bash_syntax_ok(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"bash -n failed for {path}:\n{result.stderr}"


def _run_script(name: str, workdir: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(workdir / name)],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _base_env(sandbox_bin: Path, log: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{sandbox_bin}:{env['PATH']}"
    env["SANDBOX_LOG"] = str(log)
    env["NO_COLOR"] = "1"
    return env


# ---------------------------------------------------------------------------
#  Static checks
# ---------------------------------------------------------------------------


class TestScriptsBasics:
    @pytest.mark.parametrize("name", SCRIPTS)
    def test_exists(self, name: str) -> None:
        assert _script_path(name).is_file(), f"{name} missing"

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_executable(self, name: str) -> None:
        assert _is_executable(_script_path(name)), f"{name} must be executable"

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_bash_syntax(self, name: str) -> None:
        _bash_syntax_ok(_script_path(name))


class TestInstallSh:
    def test_checks_termux(self) -> None:
        text = _read("install.sh")
        assert "is_termux" in text
        assert "com.termux" in text

    def test_runs_pkg_update_and_upgrade(self) -> None:
        text = _read("install.sh")
        assert "pkg update -y" in text
        assert "pkg upgrade -y" in text

    def test_installs_required_system_packages(self) -> None:
        text = _read("install.sh")
        assert "pkg install -y git python python-pip clang rust openssl libffi" in text
        assert "pkg install -y tur-repo" in text
        assert "pkg install -y python-numpy python-pandas" in text

    def test_installs_numpy_pandas_via_pkg_not_pip_build(self) -> None:
        # Regression: on Termux, pip building numpy/pandas from source
        # (because no wheel matches the phone's Python/libc) fails —
        # e.g. it first tries to compile cmake from source, which itself
        # fails. python-numpy comes from Termux's main repo; pandas has
        # no prebuilt package there, only via TUR (Termux User
        # Repository), so tur-repo must be subscribed first.
        text = _read("install.sh")
        assert "python-numpy" in text
        assert "python-pandas" in text
        assert "tur-repo" in text

    def test_venv_uses_system_site_packages_on_termux(self) -> None:
        # So the pkg-installed numpy/pandas above are visible inside the
        # venv, and pip install -r requirements.txt doesn't try to
        # rebuild them in isolation.
        text = _read("install.sh")
        assert "--system-site-packages" in text
        assert 'PKG_MGR" == "pkg"' in text

    def test_creates_virtualenv(self) -> None:
        text = _read("install.sh")
        assert "-m venv" in text
        assert ".venv" in text
        assert "[ ! -d .venv ]" in text or "[[ -d .venv ]]" in text

    def test_installs_requirements(self) -> None:
        text = _read("install.sh")
        # The Termux path filters pandas/numpy out of the requirements
        # file (prebuilt via pkg), so the pip install line is dynamic —
        # assert the pip invocation + the filter behaviour instead of a
        # fixed literal.
        assert "pip install -r" in text
        assert "grep -viE" in text

    def test_creates_env_from_example_never_overwrites(self) -> None:
        text = _read("install.sh")
        assert "cp .env.example .env" in text
        assert "if [[ -f .env ]]" in text

    def test_creates_data_folders(self) -> None:
        text = _read("install.sh")
        assert "mkdir -p data logs backups" in text

    def test_runs_self_check_with_pass_fail(self) -> None:
        text = _read("install.sh")
        assert "setup.sh --auto" in text
        assert "self_check" in text
        assert "PASS" in text
        assert "FAIL" in text
        assert "INSTALLATION: PASS" in text

    def test_does_not_start_bot_or_open_wizard(self) -> None:
        text = _read("install.sh")
        assert "main.py --setup" not in text
        assert "main.py" not in text

    def test_idempotent_guards(self) -> None:
        text = _read("install.sh")
        assert "already exists" in text  # .venv / .env reuse messages
        assert "kept as-is" in text


class TestRunSh:
    def test_env_guard(self) -> None:
        text = _read("run.sh")
        assert ".env.example" in text
        assert "cp .env.example .env" in text

    def test_delegates_to_termux_supervisor(self) -> None:
        text = _read("run.sh")
        assert "termux-start.sh" in text
        assert "is_termux" in text

    def test_starts_main_py_foreground(self) -> None:
        text = _read("run.sh")
        assert "main.py" in text
        assert ".venv/bin/python" in text


class TestUpdateSh:
    def test_prefers_venv_python(self) -> None:
        text = _read("update.sh")
        assert ".venv/bin/python" in text

    def test_preserves_env_and_data(self) -> None:
        text = _read("update.sh")
        assert ".env.update-backup" in text
        assert ".data-update-backup" in text
        assert "git pull" in text

    def test_termux_filters_native_builds_like_install_sh(self) -> None:
        # Regression: update.sh must NOT pip-build pandas/numpy on
        # Termux (no Android wheels → cmake/ninja/numpy/pandas source
        # build chain takes 1-3h on a phone and fails with e.g. "iconv
        # is required, but was not found"). Same pkg-prebuilt strategy
        # as install.sh.
        text = _read("update.sh")
        assert "is_termux" in text
        assert "python-numpy python-pandas" in text
        assert "grep -viE" in text

    def test_termux_rebuilds_venv_if_system_packages_invisible(self) -> None:
        # A venv created before the --system-site-packages fix can't see
        # the pkg prebuilts; update.sh must recreate it, otherwise pip
        # falls back to a source build of pandas.
        text = _read("update.sh")
        assert "import pandas, numpy, cryptography, cffi" in text
        assert "--system-site-packages" in text
        assert 'rm -rf "$SCRIPT_DIR/.venv"' in text


class TestUninstallSh:
    def test_stops_bot_and_watchdog(self) -> None:
        text = _read("uninstall.sh")
        assert "termux-start.sh --stop" in text
        assert "pkill" in text
        assert "main\\.py" in text
        assert "scripts/watchdog\\.py" in text

    def test_preserves_env_and_data_in_backup(self) -> None:
        text = _read("uninstall.sh")
        assert ".uninstall-backup-" in text
        assert 'mv .env "$BK/.env"' in text
        assert 'mv data "$BK/data"' in text
        # Never destroys the user's config / trading data directly.
        assert "rm -rf data" not in text
        assert "rm -rf .env" not in text
        assert "rm -rf data/" not in text

    def test_removes_only_generated_artifacts(self) -> None:
        text = _read("uninstall.sh")
        for artifact in (".venv", "venv", "logs", "backups", ".pytest_cache"):
            assert artifact in text
        assert "rm -rf .git" not in text
        assert "source code + .git" in text

    def test_idempotent(self) -> None:
        text = _read("uninstall.sh")
        assert "No .env to preserve" in text
        assert "No data/ to preserve" in text


# ---------------------------------------------------------------------------
#  Behavioural sandbox: fake toolchain
# ---------------------------------------------------------------------------


FAKE_PYTHON = r"""#!/usr/bin/env bash
LOG="${SANDBOX_LOG:?SANDBOX_LOG not set}"
mkdir -p "$(dirname "$LOG")"
{ echo "python invoked: $*"; echo "python cwd: $PWD"; } >> "$LOG"
case "$*" in
  *"venv"*)
    mkdir -p .venv/bin
    for b in python python3 pip pip3; do
      cp "$0" ".venv/bin/$b"
      chmod +x ".venv/bin/$b"
    done
    ;;
  *"version_info"*) echo "3.11" ;;
  *"import"*) echo "imports-ok" ;;
esac
exit 0
"""

FAKE_LOGGER = r"""#!/usr/bin/env bash
LOG="${SANDBOX_LOG:?SANDBOX_LOG not set}"
mkdir -p "$(dirname "$LOG")"
echo "$(basename "$0") $*" >> "$LOG"
exit 0
"""

FAKE_SETUP = """#!/usr/bin/env bash
echo "FAKE setup.sh --auto"
echo "  PASS  fake health check"
touch setup-ran
exit 0
"""


def _write_fake(bin_dir: Path, name: str, content: str) -> None:
    path = bin_dir / name
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _build_project(tmp_path: Path, *scripts: str) -> Path:
    """Create a fake cloned-repo dir with the given scripts + env files."""
    project = tmp_path / "project"
    project.mkdir()
    for name in scripts:
        (project / name).write_bytes(_script_path(name).read_bytes())
        (project / name).chmod((project / name).stat().st_mode | stat.S_IXUSR)
    (project / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    (project / "requirements.txt").write_bytes((ROOT / "requirements.txt").read_bytes())
    (project / "setup.sh").write_text(FAKE_SETUP)
    (project / "setup.sh").chmod(0o755)
    return project


def _build_fake_tools(tmp_path: Path, include_python: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake(bin_dir, "pkg", FAKE_LOGGER)
    _write_fake(bin_dir, "apt-get", FAKE_LOGGER)
    _write_fake(bin_dir, "git", FAKE_LOGGER)
    if include_python:
        _write_fake(bin_dir, "python", FAKE_PYTHON)
        _write_fake(bin_dir, "python3", "#!/usr/bin/env bash\nexec \"$(dirname \"$0\")/python\" \"$@\"\n")
    return bin_dir


class TestInstallBehavior:
    def test_termux_path_end_to_end(self, tmp_path: Path) -> None:
        bin_dir = _build_fake_tools(tmp_path)
        project = _build_project(tmp_path, "install.sh")
        log = tmp_path / "run1.log"

        env = _base_env(bin_dir, log)
        env["PREFIX"] = "/data/data/com.termux/files/usr"
        result = _run_script("install.sh", project, env)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "INSTALLATION: PASS" in result.stdout
        assert (project / ".venv/bin/python").exists()
        assert (project / ".env").exists()
        assert "EXCHANGE=binance" in (project / ".env").read_text()
        for d in ("data", "logs", "backups"):
            assert (project / d).is_dir()
        assert (project / "setup-ran").exists()  # self-check ran

        pkg_calls = [l for l in log.read_text().splitlines() if l.startswith("pkg ")]
        assert "pkg update -y" in pkg_calls
        assert "pkg upgrade -y" in pkg_calls
        assert "pkg install -y git python python-pip clang rust openssl libffi" in pkg_calls
        assert "pkg install -y tur-repo" in pkg_calls
        assert "pkg install -y python-numpy python-pandas" in pkg_calls
        # tur-repo (which provides pandas) must be subscribed BEFORE
        # trying to install python-pandas from it.
        assert pkg_calls.index("pkg install -y tur-repo") < pkg_calls.index(
            "pkg install -y python-numpy python-pandas"
        )

        venv_calls = [l for l in log.read_text().splitlines() if "-m venv" in l]
        assert any("--system-site-packages" in c for c in venv_calls), venv_calls

        py_calls = log.read_text()
        # On Termux, pandas/numpy are filtered into a temp requirements
        # file (prebuilt via pkg), so assert the pip install ran rather
        # than the literal "requirements.txt".
        assert "-m pip install -r" in py_calls

    def test_apt_path_end_to_end(self, tmp_path: Path) -> None:
        bin_dir = _build_fake_tools(tmp_path)
        project = _build_project(tmp_path, "install.sh")
        log = tmp_path / "apt.log"

        env = _base_env(bin_dir, log)
        env["ZETBOT_PLATFORM"] = "apt"
        result = _run_script("install.sh", project, env)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Platform: Debian/Ubuntu" in result.stdout
        assert "INSTALLATION: PASS" in result.stdout
        assert (project / ".venv/bin/python").exists()
        assert (project / ".env").exists()

        apt_calls = log.read_text().splitlines()
        assert "apt-get update -y" in apt_calls
        expected = "apt-get install -y git python3 python3-venv clang rustc cargo openssl libffi-dev"
        assert expected in apt_calls

    def test_idempotent_rerun_preserves_env_and_data(self, tmp_path: Path) -> None:
        bin_dir = _build_fake_tools(tmp_path)
        project = _build_project(tmp_path, "install.sh")
        log1 = tmp_path / "run1.log"

        env = _base_env(bin_dir, log1)
        env["PREFIX"] = "/data/data/com.termux/files/usr"
        first = _run_script("install.sh", project, env)
        assert first.returncode == 0, first.stdout + first.stderr

        # User edits .env and drops data.
        with (project / ".env").open("a") as f:
            f.write("CUSTOM_USER_VALUE=keep-me\n")
        (project / "data").mkdir(exist_ok=True)
        (project / "data/keepme.txt").write_text("sentinel\n")

        log2 = tmp_path / "run2.log"
        env2 = _base_env(bin_dir, log2)
        env2["PREFIX"] = "/data/data/com.termux/files/usr"
        env2["ZETBOT_SKIP_PKG_UPDATE"] = "1"
        second = _run_script("install.sh", project, env2)

        assert second.returncode == 0, second.stdout + second.stderr
        assert "already exists — reused" in second.stdout
        assert "kept as-is" in second.stdout
        assert "CUSTOM_USER_VALUE=keep-me" in (project / ".env").read_text()
        assert (project / "data/keepme.txt").read_text() == "sentinel\n"

        pkg2 = [l for l in log2.read_text().splitlines() if l.startswith("pkg ")]
        assert "pkg update -y" not in pkg2
        assert "pkg upgrade -y" not in pkg2


class TestUninstallBehavior:
    def test_preserves_config_and_data_removes_artifacts(self, tmp_path: Path) -> None:
        project = _build_project(tmp_path, "install.sh", "uninstall.sh")
        (project / ".venv").mkdir()
        (project / "logs").mkdir()
        (project / "backups").mkdir()
        (project / ".git").mkdir()
        (project / ".git/HEAD").write_text("ref: refs/heads/main\n")
        (project / ".env").write_text("SECRET\n")
        (project / "data").mkdir()
        (project / "data/paper_state.json").write_text('{"balance": 1}\n')

        log = tmp_path / "uninstall.log"
        result = _run_script("uninstall.sh", project, _base_env(tmp_path / "bin", log))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "UNINSTALL: DONE" in result.stdout

        assert not (project / ".venv").exists()
        assert not (project / "logs").exists()
        assert not (project / "backups").exists()
        assert not (project / ".env").exists()
        assert not (project / "data").exists()

        backups = list(project.glob(".uninstall-backup-*"))
        assert len(backups) == 1
        assert (backups[0] / ".env").read_text() == "SECRET\n"
        assert (backups[0] / "data/paper_state.json").read_text() == '{"balance": 1}\n'

        assert (project / ".git/HEAD").read_text() == "ref: refs/heads/main\n"
        assert (project / "install.sh").exists()
        assert (project / "uninstall.sh").exists()

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        project = _build_project(tmp_path, "uninstall.sh")
        (project / ".env").write_text("SECRET\n")
        (project / "data").mkdir()
        log = tmp_path / "uninstall.log"
        env = _base_env(tmp_path / "bin", log)

        first = _run_script("uninstall.sh", project, env)
        assert first.returncode == 0, first.stdout + first.stderr
        second = _run_script("uninstall.sh", project, env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "No .env to preserve" in second.stdout
        assert "No data/ to preserve" in second.stdout


class TestUpdateBehavior:
    def test_backs_up_and_uses_venv_python(self, tmp_path: Path) -> None:
        bin_dir = _build_fake_tools(tmp_path, include_python=True)
        project = _build_project(tmp_path, "update.sh")
        (project / ".git").mkdir()
        (project / ".env").write_text("KEEP=1\n")
        (project / "data").mkdir()
        (project / "data/state.json").write_text("{}")
        # A venv python exists (from a prior install.sh run).
        (project / ".venv/bin").mkdir(parents=True)
        fake = tmp_path / "bin/python"
        for b in ("python", "python3", "pip", "pip3"):
            copy = project / f".venv/bin/{b}"
            copy.write_bytes(fake.read_bytes())
            copy.chmod(0o755)

        log = tmp_path / "update.log"
        result = _run_script("update.sh", project, _base_env(bin_dir, log))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Using virtualenv" in result.stdout
        assert (project / ".env").read_text() == "KEEP=1\n"
        assert (project / ".env.update-backup").exists()
        assert (project / ".data-update-backup/state.json").read_text() == "{}"
        calls = log.read_text()
        assert "git pull" in calls
        # On Termux (which this sandbox is), update.sh uses pkg prebuilts
        # and installs a filtered requirements file — never a source
        # build of pandas/numpy. The import probe passes with the fake
        # python, so the venv is reused as-is.
        assert "pkg install -y tur-repo python-numpy python-pandas python-cryptography" in calls
        assert "-m pip install -r " in calls
        assert "import pandas, numpy, cryptography, cffi" in calls
