#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — True one-click start (from zero to a running bot in one command)
#
#  Target audience: non-technical users on Termux (Android) or a VPS. This is
#  the ONE command to get from "nothing installed" to "bot is running":
#
#      curl -fsSL <this-script-raw-url> | bash
#   or bash quickstart.sh            (if you already cloned the repo)
#
#  What it does (in order):
#    1.  Detects whether we are already inside the repo folder
#        (main.py + install.sh present, walking up to 3 parent dirs).
#        If not, clones the repo to ~/zetbot-ai (respecting
#        QUICKSTART_REPO_URL) and cd's into it.
#    2.  Runs the existing installer: bash install.sh (all system packages,
#        virtualenv, Python dependencies, .env from .env.example, folders,
#        self-check). No duplicated logic — this script reuses install.sh.
#    3.  If .env was freshly created by install.sh (never configured by the
#        user before), asks ONE interactive question with simple numbered
#        choices: 1) Indodax (IDR) or 2) Binance (USDT). Then sets
#        EXCHANGE + QUOTE_CURRENCY (+ ACCOUNT_BALANCE) consistently in .env
#        — never EXCHANGE=indodax with QUOTE_CURRENCY=USDT (that old bug
#        made the scanner find zero markets).
#        If .env already existed and was filled by the user, NOTHING is
#        asked and NOTHING is touched — it is used as-is.
#    4.  PAPER_MODE is ALWAYS true on this path. quickstart.sh can never
#        enable live trading and never asks for exchange API credentials.
#        If a pre-existing .env is configured for live trading (PAPER_MODE
#        not "true"), quickstart refuses to start the bot and points to the
#        manual path instead.
#    5.  Starts the bot: bash run.sh
#
#  Safe to re-run (idempotent): existing repos, .env, .venv and data/ are
#  reused; nothing user-owned is overwritten.
#
#  Options / env (advanced / automation):
#      QUICKSTART_REPO_URL=...       repo to clone (default: official GitHub)
#      QUICKSTART_EXCHANGE=1|2|indodax|binance   skip the interactive prompt
#      NO_COLOR=1                     plain output (no ANSI colors)
# =============================================================================

set -uo pipefail

# Non-interactive package operations: cloning/installing must never block on a
# conffile prompt when run via `curl ... | bash` (piped stdin, no TTY).
export DEBIAN_FRONTEND=noninteractive
export UCF_FORCE_CONFFOLD=1

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

has_cmd() { command -v "$1" >/dev/null 2>&1; }

is_termux() {
    [[ "${PREFIX:-}" == *"/com.termux"* ]] && return 0
    [[ -d /data/data/com.termux ]] && return 0
    return 1
}

info() { echo -e "  ${DIM}$1${NC}"; }
pass() { echo -e "  ${GREEN}$1${NC}"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; }

REPO_URL="${QUICKSTART_REPO_URL:-https://github.com/EVOZXLabs/zetbot-ai.git}"
REPO_DIR=""

# ===========================================================================
#  Step 1 — Locate or clone the repository
# ===========================================================================

find_repo_dir() {
    # Walk up to 3 parents from $PWD looking for the repo root.
    local d="$PWD" i=0
    while (( i < 4 )); do
        if [[ -f "$d/main.py" && -f "$d/install.sh" ]]; then
            REPO_DIR="$d"
            return 0
        fi
        [[ "$d" == "/" ]] && return 1
        d="$(dirname "$d")"
        ((i++))
    done
    return 1
}

ensure_git() {
    if has_cmd git; then
        return 0
    fi
    warn "git belum terpasang — memasang dulu..."
    if is_termux; then
        pkg install -y git -o Dpkg::Options::=--force-confold
    elif has_cmd apt-get; then
        env DEBIAN_FRONTEND=noninteractive apt-get install -y git -o Dpkg::Options::=--force-confold
    elif has_cmd brew; then
        brew install git
    else
        fail "Tidak bisa memasang git secara otomatis — install git lalu coba lagi."
        return 1
    fi
    has_cmd git || { fail "git masih belum tersedia setelah instalasi."; return 1; }
}

clone_repo() {
    local target="$HOME/zetbot-ai"
    if [[ -f "$target/main.py" && -f "$target/install.sh" ]]; then
        pass "Repo sudah ada di $target — dipakai ulang"
        REPO_DIR="$target"
        return 0
    fi
    if [[ -e "$target" ]] && [[ -n "$(ls -A "$target" 2>/dev/null)" ]]; then
        fail "$target sudah ada tapi bukan repo ZetBot yang lengkap."
        fail "Hapus folder itu dulu (rm -rf $target) lalu jalankan ulang."
        return 1
    fi
    ensure_git || return 1
    info "Cloning $REPO_URL → $target ..."
    if ! git clone "$REPO_URL" "$target"; then
        fail "git clone gagal — cek koneksi internet lalu coba lagi."
        return 1
    fi
    pass "Repo di-clone ke $target"
    REPO_DIR="$target"
    return 0
}

# ===========================================================================
#  Step 2 — Interactive exchange choice (only for a fresh .env)
# ===========================================================================

# Prompts with simple numbered options (1 or 2), never free-text typing.
# Works under `curl ... | bash` too: when stdin is not a terminal we try to
# re-open /dev/tty so the prompt still reaches the real terminal. If there
# is no terminal at all (tests, automation) the input is read from stdin
# and an empty/invalid answer falls back to option 1 (Indodax).
choose_exchange() {
    local choice="" exchange="" quote="" balance=""

    if [[ -n "${QUICKSTART_EXCHANGE:-}" ]]; then
        case "$(printf '%s' "$QUICKSTART_EXCHANGE" | tr '[:upper:]' '[:lower:]')" in
            indodax|1) choice="1" ;;
            binance|2) choice="2" ;;
            *)
                warn "QUICKSTART_EXCHANGE='$QUICKSTART_EXCHANGE' tidak dikenal — memakai bawaan (1) Indodax"
                choice="1"
                ;;
        esac
    else
        echo ""
        echo -e "${CYAN}${BOLD}Pilih exchange untuk PAPER TRADING (uang simulasi — aman):${NC}"
        echo -e "  ${BOLD}1)${NC} Indodax  — pasangan Rupiah (IDR)"
        echo -e "  ${BOLD}2)${NC} Binance  — pasangan USDT"
        printf "  Pilihan [1]: "

        if [[ -t 0 ]]; then
            read -r choice || true
        elif exec < /dev/tty 2>/dev/null; then
            read -r choice || true
        else
            read -r choice || true
        fi
        choice="${choice:-1}"
        if [[ "$choice" != "1" && "$choice" != "2" ]]; then
            warn "Pilihan '$choice' tidak valid — memakai bawaan (1) Indodax"
            choice="1"
        fi
    fi

    case "$choice" in
        1) exchange="indodax"; quote="IDR"; balance="1000000" ;;
        2) exchange="binance"; quote="USDT"; balance="10000" ;;
    esac

    # EXCHANGE and QUOTE_CURRENCY must always be consistent (Indodax only
    # has IDR pairs; the old EXCHANGE=indodax + QUOTE_CURRENCY=USDT combo
    # made the scanner find zero markets). ACCOUNT_BALANCE follows the
    # documented defaults for the chosen currency (1 juta IDR / 10000 USDT).
    sed -E -i.bak \
        -e "s/^[[:space:]]*EXCHANGE=.*/EXCHANGE=$exchange/" \
        -e "s/^[[:space:]]*QUOTE_CURRENCY=.*/QUOTE_CURRENCY=$quote/" \
        -e "s/^[[:space:]]*ACCOUNT_BALANCE=.*/ACCOUNT_BALANCE=$balance/" \
        .env
    rm -f .env.bak

    # PAPER_MODE is ALWAYS true here — quickstart can never enable live
    # trading, not even if .env.example ever shipped something else.
    sed -E -e "s/^[[:space:]]*PAPER_MODE=.*/PAPER_MODE=true/" -i.bak .env
    rm -f .env.bak

    pass "Konfigurasi dibuat: EXCHANGE=$exchange · QUOTE_CURRENCY=$quote · ACCOUNT_BALANCE=$balance"
    pass "PAPER_MODE=true — semua transaksi SIMULASI, tidak ada uang asli"
}

# Returns 0 when .env is safe paper mode, 1 when it is configured for live.
env_is_paper() {
    local val=""
    val="$(sed -nE 's/^[[:space:]]*PAPER_MODE[[:space:]]*=[[:space:]]*(.*)/\1/p' .env \
        | head -n1 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [[ -z "$val" || "$val" == "true" ]]
}

# ===========================================================================
#  Main
# ===========================================================================
main() {
    echo -e "${CYAN}${BOLD}"
    echo "  =============================================="
    echo "        ZetBot AI — Quick Start"
    echo "  =============================================="
    echo -e "${NC}"

    # --- Step 1: locate or clone the repo --------------------------------
    if find_repo_dir; then
        pass "Berada di dalam folder repo: $REPO_DIR"
    else
        echo -e "${BOLD}Repo belum ada — akan di-clone dulu.${NC}"
        clone_repo || exit 1
    fi
    cd "$REPO_DIR" || exit 1

    # --- Step 2: was .env already configured by the user? ----------------
    local env_preexisting=0
    if [[ -f .env ]]; then
        env_preexisting=1
    fi

    # --- Step 3: run the existing installer (reused, never duplicated) ---
    echo ""
    echo -e "${BOLD}Menjalankan installer (install.sh)...${NC}"
    if ! bash install.sh; then
        fail "Installer gagal — perbaiki pesan FAIL di atas lalu jalankan ulang."
        exit 1
    fi

    # --- Step 4: configure a fresh .env (never a pre-existing one) -------
    if (( env_preexisting == 0 )); then
        if [[ ! -f .env ]]; then
            fail ".env tidak dibuat oleh install.sh — ada yang salah."
            exit 1
        fi
        echo ""
        echo -e "${BOLD}.env baru — pilih exchange:${NC}"
        choose_exchange
    else
        echo ""
        pass ".env sudah ada sebelumnya — dipakai apa adanya, tidak diubah."
        if ! env_is_paper; then
            fail "PAPER_MODE tidak 'true' di .env (live trading sudah dikonfigurasi manual)."
            fail "quickstart hanya menjalankan PAPER MODE — ini tidak diubah."
            fail "Jalankan bot secara manual kalau kamu memang mau live: bash run.sh"
            exit 1
        fi
        pass "PAPER_MODE=true terverifikasi — aman dilanjutkan."
    fi

    # --- Step 5: start the bot ------------------------------------------
    echo ""
    echo -e "${BOLD}Memulai bot...${NC}"
    echo -e "  (stop: Ctrl+C · jalankan lagi nanti: bash run.sh)"
    exec bash run.sh
}

main "$@"
