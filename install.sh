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
            pkg update -y || return 1
            info "Running: pkg upgrade -y"
            pkg upgrade -y || return 1
            ;;
        apt)
            info "Running: apt-get update -y"
            apt-get update -y || return 1
            ;;
        brew)
            info "Running: brew update"
            brew update || return 1
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
            info "Installing: git python clang rust openssl libffi"
            pkg install -y git python clang rust openssl libffi || return 1

            # Termux does not use PyPI manylinux wheels.  cryptography is a
            # Rust-backed native package and its PyPI sdist currently asks
            # maturin/rustup for the Android target, which fails on a normal
            # Termux Python (e.g. cpython-314-aarch64-linux-android).
            # Use Termux-built packages instead and let the venv inherit them
            # via --system-site-packages below.
            info "Installing: python-cryptography python-cffi (Termux native builds)"
            pkg install -y python-cryptography python-cffi || return 1

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
            pkg install -y tur-repo || return 1
            info "Installing: python-numpy python-pandas (prebuilt — no source compile)"
            pkg install -y python-numpy python-pandas || return 1
            ;;
        apt)
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                git python3 python3-venv clang rustc cargo openssl libffi-dev \
                || return 1
            ;;
        brew)
            brew install git python@3.11 clang rust openssl libffi || return 1
            ;;
    esac
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
    info "Upgrading pip..."
    "$PY" -m pip install --upgrade pip -q 2>/dev/null || warn "pip upgrade skipped"

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
        info "Termux detected — using pkg prebuilts for numpy, pandas, cryptography, and cffi"
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
    if [[ -f setup.sh ]]; then
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
        fail "System update failed — check your internet connection"
        print_summary
        exit 1
    fi

    step 3 11 "Installing required system packages"
    if install_packages; then
        pass "Required system packages installed"
    else
        fail "Package installation failed"
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
        fail "bin/zetbot not found"
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
