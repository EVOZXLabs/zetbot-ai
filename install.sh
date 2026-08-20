#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — One-Click Installer (Termux / Linux / macOS)
#
#  Target audience: non-technical users. After cloning the repository, the
#  ONLY command that needs to be run is:
#
#      bash install.sh
#
#  What this does (in order):
#    1.  Detects the platform (Termux first — this is the primary target)
#    2.  Updates system packages (pkg / apt-get / brew)
#    3.  Installs required system packages:
#            git, python, clang, rust, openssl, libffi   (Termux)
#            + tur-repo, python-numpy, python-pandas     (Termux prebuilts)
#    4.  Creates a virtualenv (.venv/)
#    5.  Installs requirements.txt into the virtualenv
#    6.  Creates .env from .env.example (never overwrites an existing .env)
#    7.  Creates the required data folders (data/ logs/ backups/)
#    8.  Installs the global `zetbot` CLI shortcut
#    9.  Creates an optional Termux:Widget one-tap start shortcut
#        (~/.shortcuts/zetbot-start.sh — only on Termux at ~/zetbot-ai)
#    10. Runs a self-check and shows clear PASS/FAIL status
#    11. Shows what to do next
#
#  Safe to run multiple times (idempotent): nothing is overwritten, existing
#  .env / .venv / data are reused. No manual input is required. This script
#  does NOT touch trading logic and does NOT start or stop the bot.
#
#  Options / env:
#      ZETBOT_SKIP_PKG_UPDATE=1   skip "pkg update && pkg upgrade" (faster
#                                 re-runs; only package installation happens)
#      ZETBOT_PLATFORM=termux|apt|brew   force the package manager (also used
#                                 by the test-suite to exercise each branch)
#      NO_COLOR=1                 plain output (no ANSI colors)
# =============================================================================

set -uo pipefail

# Force every package-manager / dpkg operation to be NON-INTERACTIVE. This is
# what makes `curl ... | bash` safe on Termux: with a piped stdin (not a TTY),
# dpkg must never prompt for a conffile decision (Y/I/N/O/D/Z). On a fresh
# Termux install `pkg upgrade -y` reconfigures packages (e.g. openssl) whose
# conffile changed upstream; dpkg then reads stdin and dies with
# "end of file on stdin at conffile prompt". UCF_FORCE_CONFFOLD keeps
# ucf-managed configs; --force-confold (passed per-command below) keeps the
# rest. EXISTING USER CONFIGURATION IS ALWAYS PRESERVED, never overwritten.
export DEBIAN_FRONTEND=noninteractive
export UCF_FORCE_CONFFOLD=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors / symbols (auto-disable when not a TTY or NO_COLOR=1) ──────────
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    DIM='\033[2m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; DIM=''; NC=''
fi

# ── Counters ───────────────────────────────────────────────────────────────
_PASS=0
_FAIL=0
_WARN=0

step() {
    echo ""
    echo -e "${BOLD}[$1/$2] $3${NC}"
}

pass() { echo -e "  ${GREEN}PASS${NC}  $1"; ((_PASS++)) || true; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; ((_WARN++)) || true; }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; ((_FAIL++)) || true; }
info() { echo -e "  ${DIM}$1${NC}"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ── Termux detection ───────────────────────────────────────────────────────
is_termux() {
    [[ "${PREFIX:-}" == *"/com.termux"* ]] && return 0
    [[ -d /data/data/com.termux ]] && return 0
    return 1
}

# ── Non-interactive package-manager helpers ─────────────────────────────────
# A fresh Termux `pkg upgrade -y` reconfigures packages whose conffile changed
# upstream (e.g. openssl.cnf). dpkg would then ask Y/I/N/O/D/Z — but under
# `curl ... | bash` stdin is a pipe (EOF), so dpkg aborts with
# "end of file on stdin at conffile prompt". These helpers (a) always pass the
# non-interactive conffile options, (b) classify failures so the user gets an
# actionable message instead of a misleading "check your internet connection",
# and (c) verify the dpkg database afterwards and auto-recover when safe.

# Classify a package-manager failure from its captured output.
_classify_pkg_error() {
    local output="$1" cmd="$2"
    if echo "$output" | grep -qiE "end of file on stdin|conffile prompt|unable to configure|conf file"; then
        fail "${cmd} failed: dpkg asked for a CONFFILE decision but stdin is not a TTY"
        fail "  (this should be fixed now — re-run; the installer forces --force-confold)"
    elif echo "$output" | grep -qiE "Could not connect|Connection timed out|Temporary failure resolving|Name or service not known|Network is unreachable|Failed to fetch|E: Unable to fetch|404"; then
        fail "${cmd} failed: NETWORK / DNS error — check your internet connection"
    elif echo "$output" | grep -qiE "dpkg status database is locked|Could not get lock|Waiting for cache lock|Unable to acquire the dpkg lock|another process is using"; then
        fail "${cmd} failed: dpkg/apt LOCK held by another process — wait a moment and retry"
    elif echo "$output" | grep -qiE "broken packages|unmet dependencies|dependency problems|but it is not going to be installed|held broken packages"; then
        fail "${cmd} failed: BROKEN PACKAGE / dependency error"
    elif echo "$output" | grep -qiE "Unable to locate package|has no installation candidate|repository .* does not have a Release file|E: Package .* has no candidate"; then
        fail "${cmd} failed: MISSING REPOSITORY / unavailable package — try 'pkg update' first"
    elif echo "$output" | grep -qiE "Permission denied|Operation not permitted|EACCES|Read-only file system"; then
        fail "${cmd} failed: PERMISSION error — check storage permission (termux-setup-storage)"
    elif echo "$output" | grep -qiE "not supported|unsupported platform|unrecognized platform"; then
        fail "${cmd} failed: UNSUPPORTED PLATFORM"
    else
        fail "${cmd} failed — see the output above for details"
    fi
}

# Run a package-manager command, streaming its output to the terminal (so a
# long `curl | bash` run stays alive) while capturing it for error
# classification. Always passes the non-interactive conffile options.
_run_pkg_capture() {
    local label="$1"; shift
    local log
    log="$(mktemp)" || { fail "$label: could not create temp log"; return 1; }
    # shellcheck disable=SC2086
    if ! "$@" 2>&1 | tee "$log"; then
        _classify_pkg_error "$(cat "$log")" "$label"
        rm -f "$log"
        return 1
    fi
    rm -f "$log"
    return 0
}

# Verify the dpkg/package database is consistent after installing packages.
# A non-interactive conffile prompt can leave packages half-configured;
# recover automatically (keeping existing configs) when safe, else FAIL loudly.
_verify_dpkg() {
    has_cmd dpkg || return 0
    local audit
    audit="$(dpkg --audit 2>&1)" || true
    if [[ -n "$audit" ]]; then
        warn "dpkg --audit reported unconfigured packages:"
        echo "$audit" | sed 's/^/    /' >&2 || true
        info "Attempting automatic recovery (keeps existing configs): dpkg --configure -a"
        # shellcheck disable=SC2086
        if DEBIAN_FRONTEND=noninteractive dpkg --configure -a -o Dpkg::Options::=--force-confold 2>&1; then
            pass "dpkg recovery completed"
        else
            fail "dpkg --configure -a failed — packages are still broken; manual fix required"
            return 1
        fi
        audit="$(dpkg --audit 2>&1)" || true
        if [[ -n "$audit" ]]; then
            fail "dpkg --audit still reports problems after recovery"
            return 1
        fi
    fi
    return 0
}

# ── Banner ─────────────────────────────────────────────────────────────────
print_banner() {
    echo -e "${CYAN}${BOLD}"
    echo "  =============================================="
    echo "        ZetBot AI — One-Click Installer"
    echo "  =============================================="
    echo -e "${NC}"
}

# ===========================================================================
#  Step 1 — Platform detection
# ===========================================================================
detect_platform() {
    local mgr=""

    # Allow forcing the package manager (also used by the test-suite).
    case "${ZETBOT_PLATFORM:-}" in
        termux) mgr="pkg" ;;
        apt)    mgr="apt" ;;
        brew)   mgr="brew" ;;
        ""|auto) ;;
        *)
            fail "Unknown ZETBOT_PLATFORM value: ${ZETBOT_PLATFORM}"
            return 1
            ;;
    esac

    if [[ -z "$mgr" ]]; then
        if is_termux; then
            mgr="pkg"
        elif has_cmd apt-get; then
            mgr="apt"
        elif has_cmd brew; then
            mgr="brew"
        fi
    fi

    case "$mgr" in
        pkg)
            PKG_MGR="pkg"
            pass "Platform: Termux (${PREFIX:-/data/data/com.termux})"
            ;;
        apt)
            PKG_MGR="apt"
            pass "Platform: Debian/Ubuntu (using apt-get)"
            ;;
        brew)
            PKG_MGR="brew"
            pass "Platform: macOS (using Homebrew)"
            ;;
        *)
            fail "Unsupported platform — run this on Termux (Android), Debian/Ubuntu, or macOS"
            return 1
            ;;
    esac
    return 0
}

# ===========================================================================
#  Step 2 — Update system packages
# ===========================================================================
update_system() {
    if [[ "${ZETBOT_SKIP_PKG_UPDATE:-0}" == "1" ]]; then
        info "Skipping system update (ZETBOT_SKIP_PKG_UPDATE=1)"
        return 0
    fi

    case "$PKG_MGR" in
        pkg)
            info "Running: pkg update -y"
            _run_pkg_capture "pkg update" pkg update -y || return 1
            info "Running: pkg upgrade -y (non-interactive, keeps existing configs)"
            _run_pkg_capture "pkg upgrade" \
                pkg upgrade -y -o Dpkg::Options::=--force-confold || return 1
            ;;
        apt)
            info "Running: apt-get update -y"
            _run_pkg_capture "apt-get update" apt-get update -y || return 1
            info "Running: apt-get upgrade -y (non-interactive, keeps existing configs)"
            _run_pkg_capture "apt-get upgrade" \
                apt-get upgrade -y -o Dpkg::Options::=--force-confold || return 1
            ;;
        brew)
            info "Running: brew update"
            _run_pkg_capture "brew update" brew update || return 1
            ;;
    esac
    return 0
}

# ===========================================================================
#  Step 3 — Install system packages
# ===========================================================================
install_packages() {
    case "$PKG_MGR" in
        pkg)
            # Termux package names — matches INSTALL.md / spec exactly.
            info "Installing: git python python-pip clang rust openssl libffi"
            _run_pkg_capture "pkg install (base)" \
                pkg install -y git python python-pip clang rust openssl libffi \
                -o Dpkg::Options::=--force-confold || return 1

            # Termux does not use PyPI manylinux wheels.  cryptography is a
            # Rust-backed native package and its PyPI sdist currently asks
            # maturin/rustup for the Android target, which fails on a normal
            # Termux Python (e.g. cpython-314-aarch64-linux-android).
            # Use Termux-built packages instead and let the venv inherit them
            # via --system-site-packages below.
            info "Installing: python-cryptography (Termux native build)"
            _run_pkg_capture "pkg install (cryptography)" \
                pkg install -y python-cryptography \
                -o Dpkg::Options::=--force-confold || return 1

            # numpy/pandas: PyPI has no manylinux/musllinux wheel that's
            # compatible with Termux's Python + Android's bionic libc, so
            # `pip install numpy/pandas` falls back to compiling from
            # source — which needs to bootstrap `cmake`/`ninja` itself
            # first and frequently fails outright on a phone. Termux's
            # main repo ships a prebuilt python-numpy, but NOT pandas —
            # pandas is only available prebuilt via the community Termux
            # User Repository (TUR: https://github.com/termux-user-repository/tur),
            # so subscribe to that first, then install both from pkg.
            # setup_venv() below makes sure the venv can see them
            # (--system-site-packages).
            info "Installing: tur-repo (provides a prebuilt python-pandas)"
            _run_pkg_capture "pkg install (tur-repo)" \
                pkg install -y tur-repo \
                -o Dpkg::Options::=--force-confold || return 1
            info "Installing: python-numpy python-pandas (prebuilt — no source compile)"
            _run_pkg_capture "pkg install (numpy/pandas)" \
                pkg install -y python-numpy python-pandas \
                -o Dpkg::Options::=--force-confold || return 1

            # tmux: required by scripts/termux-start.sh (the supervised
            # bot launcher that keeps the bot alive when Termux is
            # minimized). termux-api: required for termux-wake-lock so
            # the bot survives Android Doze mode. cmake: needed by
            # native pip builds (pycares, etc.) that compile C extensions.
            info "Installing: tmux termux-api cmake (session supervisor + wake-lock + native builds)"
            _run_pkg_capture "pkg install (tmux/termux-api/cmake)" \
                pkg install -y tmux termux-api cmake \
                -o Dpkg::Options::=--force-confold || return 1
            ;;
        apt)
            info "Installing Debian/Ubuntu packages (non-interactive, keeps existing configs)"
            _run_pkg_capture "apt-get install" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
                git python3 python3-venv clang rustc cargo openssl libffi-dev \
                -o Dpkg::Options::=--force-confold || return 1
            ;;
        brew)
            _run_pkg_capture "brew install" \
                brew install git python@3.11 clang rust openssl libffi || return 1
            ;;
    esac

    # Verify the package database is consistent after installing — a
    # non-interactive conffile prompt can leave packages half-configured.
    _verify_dpkg || return 1
    return 0
}

# ===========================================================================
#  Step 4 — Virtualenv
# ===========================================================================
setup_venv() {
    local py="" ver="" major="" minor=""

    for c in python3 python; do
        if has_cmd "$c"; then py="$c"; break; fi
    done
    if [[ -z "$py" ]]; then
        fail "No Python interpreter found — install python (Termux: pkg install python)"
        return 1
    fi

    ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
    if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
        major="${BASH_REMATCH[1]}"
        minor="${BASH_REMATCH[2]}"
        if (( 10#$major > 3 )) || (( 10#$major == 3 && 10#$minor >= 10 )); then
            pass "Python ${ver} (${py})"
        else
            fail "Python ${ver} found — 3.10+ is required"
            return 1
        fi
    else
        fail "Could not determine Python version (${py})"
        return 1
    fi

    if [[ -d .venv ]]; then
        pass ".venv already exists — reused"
    else
        info "Creating virtualenv (.venv/)..."
        # On Termux, --system-site-packages lets the venv see the
        # prebuilt python-numpy/python-pandas installed via pkg above
        # (step 3), so pip doesn't try to rebuild them from source inside
        # the isolated venv. Other platforms (apt/brew) get manylinux/
        # macOS wheels from PyPI fine, so they keep the normal isolated
        # venv.
        local venv_flags=()
        if [[ "$PKG_MGR" == "pkg" ]]; then
            venv_flags+=(--system-site-packages)
        fi
        if ! "$py" -m venv "${venv_flags[@]}" .venv; then
            fail "Could not create virtualenv — python3-venv may be missing (Debian: install it)"
            return 1
        fi
        pass "Virtualenv created (.venv/)"
    fi
    PY="$SCRIPT_DIR/.venv/bin/python"
    return 0
}

# ===========================================================================
#  Step 5 — Install Python dependencies
# ===========================================================================
install_requirements() {
    if [[ ! -f requirements.txt ]]; then
        fail "requirements.txt not found"
        return 1
    fi
    # Termux owns pip through the python-pip package. Do not upgrade pip
    # with PyPI here: Termux intentionally prevents this because replacing
    # the packaged pip can break the Python installation.
    if [[ "$PKG_MGR" == "pkg" ]]; then
        info "Termux detected — keeping the system-managed pip"
    else
        info "Upgrading pip..."
        "$PY" -m pip install --upgrade pip -q 2>/dev/null || warn "pip upgrade skipped"
    fi

    local req_file="requirements.txt"
    local tmp_req=""

    # Termux does not consume the usual Linux wheels from PyPI.  Keep native
    # packages supplied by Termux out of pip's build path.  In particular,
    # cryptography currently falls back to an sdist whose maturin bootstrap
    # tries to use rustup with the unsupported Android target
    # aarch64-unknown-linux-android.  The Termux python-cryptography package
    # is already built for Android and is exposed inside our venv through
    # --system-site-packages.
    if [[ "$PKG_MGR" == "pkg" ]]; then
        tmp_req="$(mktemp)"
        grep -viE '^(pandas|numpy|cryptography|cffi)([[:space:]]*(==|>=|<=|~=|>|<).*)?$' "$req_file" > "$tmp_req"
        req_file="$tmp_req"
        info "Termux detected — using pkg prebuilts for numpy, pandas, and cryptography"
    fi

    info "Installing $req_file (this can take a few minutes)..."
    if "$PY" -m pip install -r "$req_file"; then
        pass "Python dependencies installed"
    else
        fail "pip install failed — re-run: bash install.sh"
        rm -f "$tmp_req"
        return 1
    fi
    rm -f "$tmp_req"

    # Optional: on-chain / Web3 deps (solders requires Rust toolchain;
    # fails on Termux — safe to skip for CEX-only trading).
    if [[ -f requirements-onchain.txt ]]; then
        info "Installing on-chain dependencies (optional — safe to skip)..."
        if "$PY" -m pip install -r requirements-onchain.txt -q 2>/dev/null; then
            pass "On-chain dependencies installed"
        else
            warn "On-chain deps (solders/web3) skipped — Rust toolchain missing. CEX trading unaffected."
        fi
    fi
    return 0
}

# ===========================================================================
#  Step 6 — .env configuration
# ===========================================================================
setup_env() {
    if [[ -f .env ]]; then
        pass ".env already present — kept as-is"
        return 0
    fi
    if [[ ! -f .env.example ]]; then
        fail ".env.example not found"
        return 1
    fi
    cp .env.example .env
    chmod 600 .env 2>/dev/null || true
    pass "Created .env from .env.example (paper trading is on by default)"
    info "Edit credentials later with: nano .env"
    return 0
}

# ===========================================================================
#  Step 7 — Data folders
# ===========================================================================
setup_folders() {
    mkdir -p data logs backups
    pass "Folders ready: data/ logs/ backups/"
    return 0
}

# ===========================================================================
#  Step 8 — Optional Termux:Widget shortcut (one-tap start from the home
#  screen — only when running on Termux with the repo at ~/zetbot-ai)
# ===========================================================================
setup_widget() {
    if ! is_termux; then
        info "Skipping Termux:Widget shortcut (not on Termux)"
        return 0
    fi
    if [[ "$HOME" != *"/com.termux"* ]]; then
        info "Skipping Termux:Widget shortcut (HOME is not a Termux home)"
        return 0
    fi
    if [[ "$SCRIPT_DIR" != "$HOME/zetbot-ai" ]]; then
        info "Skipping Termux:Widget shortcut (repo not at ~/zetbot-ai)"
        return 0
    fi
    local short_dir="$HOME/.shortcuts"
    local short_file="$short_dir/zetbot-start.sh"
    if [[ -f "$short_file" ]]; then
        pass "Termux:Widget shortcut already present — kept ($short_file)"
        return 0
    fi
    if ! mkdir -p "$short_dir"; then
        fail "Could not create $short_dir"
        return 1
    fi
    # A widget on the home screen (Termux:Widget app) taps this to start
    # the bot without opening Termux first. Termux:Widget is optional and
    # never installed automatically — see QUICKSTART.md.
    printf '#!/usr/bin/env bash\ncd ~/zetbot-ai && bash run.sh\n' > "$short_file"
    chmod 700 "$short_file"
    pass "Termux:Widget shortcut created — install the Termux:Widget app,"
    pass "  add a widget on the home screen, tap it to start the bot."
    return 0
}

# ===========================================================================
#  Step 9 — Self-check (clear PASS/FAIL)
# ===========================================================================
self_check() {
    local deps_ok=0

    if "$PY" -c "import sys; import ccxt, requests, dotenv, colorama; import cryptography, cffi" >/dev/null 2>&1; then
        pass "Dependencies importable (ccxt, requests, dotenv, colorama, cryptography, cffi)"
    else
        fail "Core dependencies cannot be imported — re-run: bash install.sh"
        deps_ok=1
    fi

    if [[ -f .env ]]; then
        pass ".env present"
    else
        fail ".env missing"
        deps_ok=1
    fi

    if [[ -d data ]] && [[ -d logs ]]; then
        pass "Runtime folders present"
    else
        fail "Runtime folders missing"
        deps_ok=1
    fi

    # Full health report (reuses setup.sh --auto with the venv active).
    # Skipped during quickstart (ZETBOT_SKIP_HEALTHCHECK=1) to avoid
    # confusing output on a fresh install — the user can run it manually.
    if [[ -f setup.sh ]] && [[ "${ZETBOT_SKIP_HEALTHCHECK:-0}" != "1" ]]; then
        info "Running full health check (setup.sh --auto)..."
        if VIRTUAL_ENV="$SCRIPT_DIR/.venv" \
                PATH="$SCRIPT_DIR/.venv/bin:$PATH" \
                bash setup.sh --auto; then
            pass "Health check passed"
        else
            warn "Health check reported issues (see the report above)"
        fi
    fi
    return "$deps_ok"
}

# ===========================================================================
#  Step 9 — Summary
# ===========================================================================
print_summary() {
    local total=$(( _PASS + _FAIL + _WARN ))
    echo ""
    echo -e "${BOLD}  Installer summary: ${GREEN}${_PASS} passed${NC}  ${RED}${_FAIL} failed${NC}  ${YELLOW}${_WARN} warnings${NC}  (${total} checks)"
    echo ""

    if (( _FAIL == 0 )); then
        echo -e "  ${GREEN}${BOLD}  INSTALLATION: PASS${NC}"
        echo ""
        echo -e "  ${BOLD}Next steps:${NC}"
        echo -e "    zetbot start      → start the bot"
        echo -e "    zetbot status     → show bot status"
        echo -e "    zetbot logs       → follow bot logs"
        echo -e "    zetbot stop       → stop the bot"
        echo -e "    bash update.sh    → update the bot"
        echo -e "    bash uninstall.sh → remove the bot (config/data preserved)"
        echo -e "    nano .env         → edit exchange / Telegram credentials"
        echo ""
        return 0
    fi

    echo -e "  ${RED}${BOLD}  INSTALLATION: FAIL (${_FAIL} problem(s))${NC}"
    echo ""
    echo -e "  Fix the failures above, then re-run: bash install.sh"
    echo -e "  (re-running is safe — nothing already done is lost)"
    echo ""
    return 1
}

# ===========================================================================
#  Main
# ===========================================================================
main() {
    print_banner

    step 1 11 "Checking platform (Termux / Linux / macOS)"
    if ! detect_platform; then
        print_summary
        exit 1
    fi

    step 2 11 "Updating system packages"
    if update_system; then
        pass "System packages updated"
    else
        fail "System update failed — see the error above for the exact cause (NOT necessarily a network problem)"
        print_summary
        exit 1
    fi

    step 3 11 "Installing required system packages"
    if install_packages; then
        pass "Required system packages installed"
    else
        fail "Package installation failed — see the error above for the exact cause"
        print_summary
        exit 1
    fi

    step 4 11 "Creating virtual environment"
    if ! setup_venv; then
        print_summary
        exit 1
    fi

    step 5 11 "Installing Python dependencies"
    if ! install_requirements; then
        print_summary
        exit 1
    fi

    step 6 11 "Creating .env (if needed)"
    setup_env || true

    step 7 11 "Creating data folders"
    setup_folders

    step 8 11 "Installing ZetBot CLI shortcut"
    if [[ -f "$SCRIPT_DIR/bin/zetbot" ]]; then
        chmod +x "$SCRIPT_DIR/bin/zetbot"
        if [[ -n "${PREFIX:-}" ]]; then
            if ln -sf "$SCRIPT_DIR/bin/zetbot" "$PREFIX/bin/zetbot" 2>/dev/null; then
                pass "ZetBot CLI installed: zetbot"
            else
                warn "Could not create $PREFIX/bin/zetbot"
            fi
        else
            warn "PREFIX is not set — global 'zetbot' shortcut was not created"
        fi
    else
        warn "bin/zetbot not found — skipping 'zetbot' shortcut (optional)"
    fi

    step 9 11 "Creating optional Termux:Widget shortcut"
    setup_widget

    step 10 11 "Running self-check"
    self_check || true

    step 11 11 "Summary"
    print_summary
    exit $(( _FAIL > 0 ? 1 : 0 ))
}

main "$@"
