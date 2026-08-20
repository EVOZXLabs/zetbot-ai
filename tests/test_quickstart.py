"""Tests for the true one-click installer (quickstart.sh).

Two layers, following the pattern of test_installer_scripts.py:

* Static checks (offline, deterministic) — quickstart.sh exists, is
  executable, passes ``bash -n``, clones the repo when needed, reuses
  install.sh, always keeps PAPER_MODE=true, never asks for API keys, and
  starts the bot via run.sh at the end.

* Behavioural sandbox tests — quickstart.sh is executed inside a tmp
  workdir with FAKE ``pkg`` / ``apt-get`` / ``git`` / ``python`` binaries
  on the PATH and a sandbox $HOME, so the whole "zero → cloned repo →
  install → .env → run.sh" flow runs end-to-end with no network and no
  mutation of the real system. The interactive exchange prompt is driven
  through a real pseudo-terminal (pty), and the non-interactive path is
  covered via QUICKSTART_EXCHANGE.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPO_URL = "https://github.com/EVOZXLabs/zetbot-ai.git"

_CLONE_FILES = (
    "main.py",
    "install.sh",
    "run.sh",
    "update.sh",
    "uninstall.sh",
    "quickstart.sh",
    ".env.example",
    "requirements.txt",
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _bash_syntax_ok(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"bash -n failed for {path}:\n{result.stderr}"


def _write_fake(bin_dir: Path, name: str, content: str) -> None:
    path = bin_dir / name
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _build_fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_fake(bin_dir, "pkg", FAKE_LOGGER)
    _write_fake(bin_dir, "apt-get", FAKE_LOGGER)
    _write_fake(bin_dir, "git", FAKE_GIT)
    _write_fake(bin_dir, "python", FAKE_PYTHON)
    _write_fake(bin_dir, "python3", "#!/usr/bin/env bash\nexec \"$(dirname \"$0\")/python\" \"$@\"\n")
    return bin_dir


def _base_env(bin_dir: Path, log: Path, home: Path, termux: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SANDBOX_LOG"] = str(log)
    env["SANDBOX_SRC"] = str(ROOT)
    env["HOME"] = str(home)
    if termux:
        env["PREFIX"] = "/data/data/com.termux/files/usr"
    env["NO_COLOR"] = "1"
    return env


def _home_path(tmp_path: Path) -> Path:
    return tmp_path / "data/data/com.termux/files/home"


def _make_workdir(tmp_path: Path) -> tuple[Path, Path]:
    home = _home_path(tmp_path)
    home.mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    shutil.copy2(ROOT / "quickstart.sh", workdir / "quickstart.sh")
    (workdir / "quickstart.sh").chmod(0o755)
    return workdir, home


def _run_quickstart(
    workdir: Path, env: dict[str, str], pty_answer: str | None = None,
) -> tuple[int, str]:
    """Run quickstart.sh. With ``pty_answer`` the interactive prompt is
    answered through a real pseudo-terminal (deterministic interactive
    coverage); otherwise run non-interactively."""
    if pty_answer is None:
        result = subprocess.run(
            ["bash", "quickstart.sh"],
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode, result.stdout + result.stderr

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    proc = subprocess.Popen(
        ["bash", "quickstart.sh"],
        cwd=str(workdir),
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=subprocess.STDOUT,
    )
    os.close(slave)

    def _drain() -> str:
        chunk = b""
        while True:
            try:
                data = os.read(master, 4096)
            except (BlockingIOError, OSError):
                break
            if not data:
                break
            chunk += data
        return chunk.decode("utf-8", "replace")

    out = ""
    answered = False
    deadline = time.time() + 300
    while time.time() < deadline:
        if proc.poll() is not None:
            out += _drain()
            break
        ready, _, _ = select.select([master], [], [], 0.5)
        if master in ready:
            try:
                data = os.read(master, 4096)
            except (BlockingIOError, OSError):
                data = b""
            if data:
                out += data.decode("utf-8", "replace")
                if not answered and "Pilihan [1]:" in out:
                    os.write(master, (pty_answer + "\n").encode())
                    answered = True
    proc.wait(timeout=10)
    out += _drain()
    try:
        os.close(master)
    except OSError:
        pass
    return proc.returncode, out


def _materialize_repo(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in _CLONE_FILES:
        shutil.copy2(ROOT / f, dest / f)
    (dest / "install.sh").chmod(0o755)
    (dest / "run.sh").chmod(0o755)
    (dest / "quickstart.sh").chmod(0o755)
    (dest / "setup.sh").write_text(FAKE_SETUP)
    (dest / "setup.sh").chmod(0o755)


# ---------------------------------------------------------------------------
#  Fake toolchain
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

# `git clone <url> <dir>` materializes the repo into <dir> from the real
# checkout (SANDBOX_SRC). SANDBOX_SETUP / SANDBOX_ENV_EXAMPLE let tests
# override setup.sh / .env.example in the "cloned" copy.
FAKE_GIT = r"""#!/usr/bin/env bash
LOG="${SANDBOX_LOG:?SANDBOX_LOG not set}"
mkdir -p "$(dirname "$LOG")"
echo "git $*" >> "$LOG"
case "$1" in
  clone)
    if [[ "$#" -ge 3 ]]; then target="$3"; else target="$(basename "$2" .git)"; fi
    mkdir -p "$target"
    src="${SANDBOX_SRC:?SANDBOX_SRC not set}"
    for f in main.py install.sh run.sh update.sh uninstall.sh quickstart.sh .env.example requirements.txt; do
      if [[ -e "$src/$f" ]]; then cp -r "$src/$f" "$target/"; fi
    done
    chmod +x "$target/install.sh" "$target/run.sh" "$target/quickstart.sh" 2>/dev/null || true
    if [[ -n "${SANDBOX_SETUP:-}" ]]; then
      cp "$SANDBOX_SETUP" "$target/setup.sh"
      chmod +x "$target/setup.sh"
    fi
    if [[ -n "${SANDBOX_ENV_EXAMPLE:-}" ]]; then
      cp "$SANDBOX_ENV_EXAMPLE" "$target/.env.example"
    fi
    echo "cloned to $target" >> "$LOG"
    ;;
esac
exit 0
"""

FAKE_SETUP = """#!/usr/bin/env bash
echo "FAKE setup.sh --auto"
echo "  PASS  fake health check"
touch setup-ran
exit 0
"""


# ---------------------------------------------------------------------------
#  Static checks
# ---------------------------------------------------------------------------


class TestQuickstartStatic:
    def test_exists(self) -> None:
        assert (ROOT / "quickstart.sh").is_file()

    def test_executable(self) -> None:
        path = ROOT / "quickstart.sh"
        assert bool(path.stat().st_mode & stat.S_IXUSR), "quickstart.sh must be executable"

    def test_bash_syntax(self) -> None:
        _bash_syntax_ok(ROOT / "quickstart.sh")

    def test_clones_repo_when_not_present(self) -> None:
        text = _read("quickstart.sh")
        assert "git clone" in text
        assert "QUICKSTART_REPO_URL" in text
        assert REPO_URL in text

    def test_reuses_installer(self) -> None:
        text = _read("quickstart.sh")
        assert "bash install.sh" in text

    def test_starts_bot_at_the_end(self) -> None:
        text = _read("quickstart.sh")
        assert "bash run.sh" in text

    def test_asks_simple_numbered_exchange_choice(self) -> None:
        text = _read("quickstart.sh")
        assert "Indodax" in text
        assert "Binance" in text
        # Non-interactive: no prompt, defaults to Indodax
        assert "Pilih exchange" not in text or "QUICKSTART_EXCHANGE" in text

    def test_never_sets_paper_mode_false(self) -> None:
        text = _read("quickstart.sh")
        assert "PAPER_MODE=false" not in text

    def test_always_forces_paper_mode_true(self) -> None:
        text = _read("quickstart.sh")
        assert "PAPER_MODE=true" in text

    def test_never_asks_for_api_credentials(self) -> None:
        text = _read("quickstart.sh")
        assert "API_KEY" not in text
        assert "API_SECRET" not in text

    def test_respects_preexisting_env(self) -> None:
        text = _read("quickstart.sh")
        assert "env_preexisting" in text
        assert "sudah ada sebelumnya" in text


# ---------------------------------------------------------------------------
#  Behavioural: full quickstart flow in a sandbox
# ---------------------------------------------------------------------------


class TestQuickstartBehavior:
    def _fresh_env(self, tmp_path: Path, log: Path) -> tuple[Path, dict[str, str]]:
        workdir, home = _make_workdir(tmp_path)
        bin_dir = _build_fake_tools(tmp_path)
        env = _base_env(bin_dir, log, home)
        setup = tmp_path / "setup.sh"
        setup.write_text(FAKE_SETUP)
        setup.chmod(0o755)
        env["SANDBOX_SETUP"] = str(setup)
        return workdir, env

    def _read_env(self, tmp_path: Path) -> str:
        return (_home_path(tmp_path) / "zetbot-ai" / ".env").read_text()

    def test_from_zero_to_running_indodax(self, tmp_path: Path) -> None:
        log = tmp_path / "flow.log"
        workdir, env = self._fresh_env(tmp_path, log)

        rc, out = _run_quickstart(workdir, env, pty_answer="1")

        assert rc == 0, out
        assert "INSTALLATION: PASS" in out
        assert "Memulai bot" in out

        repo = _home_path(tmp_path) / "zetbot-ai"
        assert (repo / "main.py").exists(), "repo must be cloned"
        assert (repo / "install.sh").exists()

        env_text = self._read_env(tmp_path)
        assert "EXCHANGE=indodax" in env_text
        assert "QUOTE_CURRENCY=IDR" in env_text
        assert "ACCOUNT_BALANCE=1000000" in env_text
        assert "PAPER_MODE=true" in env_text

        # run.sh was invoked at the end (fake python ran main.py).
        log_text = log.read_text()
        assert "python invoked: main.py" in log_text
        assert "git clone" in log_text

        # install.sh created the optional Termux:Widget shortcut.
        shortcut = _home_path(tmp_path) / ".shortcuts" / "zetbot-start.sh"
        assert shortcut.exists()
        assert "bash run.sh" in shortcut.read_text()

    def test_interactive_choice_binance_via_terminal(self, tmp_path: Path) -> None:
        log = tmp_path / "binance.log"
        workdir, env = self._fresh_env(tmp_path, log)
        env["QUICKSTART_EXCHANGE"] = "2"

        rc, out = _run_quickstart(workdir, env)

        assert rc == 0, out
        env_text = self._read_env(tmp_path)
        assert "EXCHANGE=binance" in env_text
        assert "QUOTE_CURRENCY=USDT" in env_text
        assert "ACCOUNT_BALANCE=10000" in env_text
        assert "PAPER_MODE=true" in env_text

    def test_env_override_binance(self, tmp_path: Path) -> None:
        log = tmp_path / "override.log"
        workdir, env = self._fresh_env(tmp_path, log)
        env["QUICKSTART_EXCHANGE"] = "binance"

        rc, out = _run_quickstart(workdir, env)

        assert rc == 0, out
        env_text = self._read_env(tmp_path)
        assert "EXCHANGE=binance" in env_text
        assert "QUOTE_CURRENCY=USDT" in env_text
        assert "PAPER_MODE=true" in env_text

    def test_invalid_env_override_falls_back_to_indodax(self, tmp_path: Path) -> None:
        log = tmp_path / "invalid.log"
        workdir, env = self._fresh_env(tmp_path, log)
        env["QUICKSTART_EXCHANGE"] = "kucoin"

        rc, out = _run_quickstart(workdir, env)

        assert rc == 0, out
        assert "tidak dikenal" in out
        env_text = self._read_env(tmp_path)
        assert "EXCHANGE=indodax" in env_text
        assert "QUOTE_CURRENCY=IDR" in env_text

    def test_paper_mode_true_even_if_env_example_says_false(self, tmp_path: Path) -> None:
        poisoned = tmp_path / "env.example.poisoned"
        poisoned.write_text(
            "PAPER_MODE=false\nEXCHANGE=binance\nQUOTE_CURRENCY=USDT\nACCOUNT_BALANCE=10000\n"
        )
        log = tmp_path / "poisoned.log"
        workdir, env = self._fresh_env(tmp_path, log)
        env["SANDBOX_ENV_EXAMPLE"] = str(poisoned)
        env["QUICKSTART_EXCHANGE"] = "binance"

        rc, out = _run_quickstart(workdir, env)

        assert rc == 0, out
        assert "PAPER_MODE=true" in self._read_env(tmp_path)
        assert "PAPER_MODE=true" not in poisoned.read_text()  # never edits the example

    def test_rerun_with_preexisting_env_no_prompt_no_overwrite(self, tmp_path: Path) -> None:
        home = _home_path(tmp_path)
        setup = tmp_path / "setup.sh"
        setup.write_text(FAKE_SETUP)
        setup.chmod(0o755)

        log1 = tmp_path / "run1.log"
        env1 = _base_env(_build_fake_tools(tmp_path), log1, home)
        env1["SANDBOX_SETUP"] = str(setup)
        workdir, _ = _make_workdir(tmp_path)
        rc1, out1 = _run_quickstart(workdir, env1, pty_answer="1")
        assert rc1 == 0, out1

        # User edits .env (their own values) and adds trading data.
        env_file = home / "zetbot-ai" / ".env"
        with env_file.open("a") as f:
            f.write("CUSTOM_USER_VALUE=keep-me\n")
        data_dir = home / "zetbot-ai" / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "keepme.txt").write_text("sentinel\n")

        log2 = tmp_path / "run2.log"
        env2 = _base_env(_build_fake_tools(tmp_path), log2, home)
        env2["SANDBOX_SETUP"] = str(setup)
        rc2, out2 = _run_quickstart(workdir, env2)

        assert rc2 == 0, out2
        assert "Pilih exchange" not in out2, "must not prompt on a pre-existing .env"
        assert "sudah ada sebelumnya" in out2
        text = env_file.read_text()
        assert "CUSTOM_USER_VALUE=keep-me" in text
        assert "EXCHANGE=indodax" in text
        assert "PAPER_MODE=true" in text
        assert (data_dir / "keepme.txt").read_text() == "sentinel\n"
        assert "python invoked: main.py" in log2.read_text(), "run.sh must start the bot"
        assert "git clone" not in log2.read_text(), "must not re-clone"

    def test_preexisting_live_env_aborts_without_starting(self, tmp_path: Path) -> None:
        home = _home_path(tmp_path)
        setup = tmp_path / "setup.sh"
        setup.write_text(FAKE_SETUP)
        setup.chmod(0o755)

        log1 = tmp_path / "run1.log"
        env1 = _base_env(_build_fake_tools(tmp_path), log1, home)
        env1["SANDBOX_SETUP"] = str(setup)
        workdir, _ = _make_workdir(tmp_path)
        rc1, out1 = _run_quickstart(workdir, env1, pty_answer="1")
        assert rc1 == 0, out1

        # User manually configured live trading (outside quickstart's scope).
        env_file = home / "zetbot-ai" / ".env"
        text = env_file.read_text().replace("PAPER_MODE=true", "PAPER_MODE=false")
        env_file.write_text(text)

        log2 = tmp_path / "run2.log"
        env2 = _base_env(_build_fake_tools(tmp_path), log2, home)
        env2["SANDBOX_SETUP"] = str(setup)
        rc2, out2 = _run_quickstart(workdir, env2)

        assert rc2 != 0
        assert "PAPER_MODE" in out2
        assert "Memulai bot" not in out2, "must NOT start the bot for a live-configured .env"
        assert "python invoked: main.py" not in log2.read_text()
        assert "PAPER_MODE=false" in env_file.read_text(), "must never overwrite a user .env"


# ---------------------------------------------------------------------------
#  Behavioural: Termux:Widget shortcut (created by install.sh)
# ---------------------------------------------------------------------------


    def test_piped_stdin_defaults_to_indodax_and_completes(self, tmp_path: Path) -> None:
        # Reproduces `printf '' | bash quickstart.sh` (curl | bash): stdin is
        # a pipe, not a TTY. The installer must not block, and the exchange
        # prompt must fall back to the default (Indodax) without a hang.
        log = tmp_path / "piped.log"
        workdir, env = self._fresh_env(tmp_path, log)
        proc = subprocess.run(
            ["bash", "quickstart.sh"],
            cwd=str(workdir),
            env=env,
            input="",                 # simulate `printf '' | bash quickstart.sh`
            start_new_session=True,   # detach so /dev/tty is unavailable -> no block
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "INSTALLATION: PASS" in proc.stdout
        assert "Memulai bot" in proc.stdout
        env_text = self._read_env(tmp_path)
        assert "EXCHANGE=indodax" in env_text
        assert "PAPER_MODE=true" in env_text
        assert "python invoked: main.py" in log.read_text()


class TestWidgetShortcut:
    def _install_env(
        self, tmp_path: Path, repo: Path, log: Path, termux: bool = True,
    ) -> dict[str, str]:
        bin_dir = _build_fake_tools(tmp_path)
        home = repo.parent
        env = _base_env(bin_dir, log, home, termux=termux)
        env["ZETBOT_SKIP_PKG_UPDATE"] = "1"
        return env

    def _run_install(self, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "install.sh"],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_created_when_repo_at_termux_home(self, tmp_path: Path) -> None:
        home = _home_path(tmp_path)
        repo = home / "zetbot-ai"
        _materialize_repo(repo)
        log = tmp_path / "widget.log"
        env = self._install_env(tmp_path, repo, log)

        first = self._run_install(repo, env)
        assert first.returncode == 0, first.stdout + first.stderr
        shortcut = home / ".shortcuts" / "zetbot-start.sh"
        assert shortcut.exists(), first.stdout
        assert "bash run.sh" in shortcut.read_text()

        second = self._run_install(repo, env)
        assert second.returncode == 0
        assert "already present — kept" in second.stdout

    def test_skipped_when_repo_not_at_termux_home(self, tmp_path: Path) -> None:
        repo = tmp_path / "project"
        _materialize_repo(repo)
        log = tmp_path / "skip1.log"
        env = self._install_env(tmp_path, repo, log)

        result = self._run_install(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (repo.parent / ".shortcuts").exists(), result.stdout

    def test_skipped_on_non_termux(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = home / "zetbot-ai"
        _materialize_repo(repo)
        log = tmp_path / "skip2.log"
        env = self._install_env(tmp_path, repo, log, termux=False)

        result = self._run_install(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (home / ".shortcuts").exists(), result.stdout
