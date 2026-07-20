#!/usr/bin/env bash
# =============================================================================
#  ZetBot AI — Production Setup & Health Check
#  Version : 1.0.0
#  License : Proprietary
#
#  Safe to run multiple times (idempotent).
#  Never overwrites existing configuration without explicit user consent.
#
#  Usage:
#      bash setup.sh              # interactive mode
#      bash setup.sh --auto       # non-interactive (accept defaults)
#      bash setup.sh --json       # machine-readable JSON output
# =============================================================================
set -euo pipefail

# ─── Version ────────────────────────────────────────────────────────────────
readonly SETUP_VERSION="1.0.0"
readonly PROJECT_NAME="ZetBot AI"

# ─── Timing ─────────────────────────────────────────────────────────────────
_SETUP_START=$(date +%s%N 2>/dev/null || date +%s)

# ─── Flags ──────────────────────────────────────────────────────────────────
AUTO_MODE=false
JSON_MODE=false

for arg in "$@"; do
    case "$arg" in
        --auto)  AUTO_MODE=true  ;;
        --json)  JSON_MODE=true  ;;
        --help|-h)
            echo "Usage: bash setup.sh [--auto] [--json] [--help]"
            echo "  --auto   Non-interactive: accept all defaults"
            echo "  --json   Output results as JSON (for programmatic use)"
            echo "  --help   Show this help message"
            exit 0
            ;;
    esac
done

# =============================================================================
#  §0  COLOR PALETTE & SYMBOLS
# =============================================================================
_setup_colors() {
    if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
        RED='\033[0;31m'
        GREEN='\033[0;32m'
        YELLOW='\033[0;33m'
        BLUE='\033[0;34m'
        MAGENTA='\033[0;35m'
        CYAN='\033[0;36m'
        WHITE='\033[1;37m'
        DIM='\033[2m'
        BOLD='\033[1m'
        NC='\033[0m'
    else
        RED='' GREEN='' YELLOW='' BLUE='' MAGENTA='' CYAN=''
        WHITE='' DIM='' BOLD='' NC=''
    fi
}
_setup_colors

# Unicode symbols (fallback to ASCII if locale is broken)
SYM_OK="✔"
SYM_FAIL="✘"
SYM_WARN="⚠"
SYM_INFO="ℹ"
SYM_ARROW="→"
SYM_DOT="●"
SYM_STAR="★"
SYM_ROCKET="🚀"
SYM_SHIELD="🛡"
SYM_GEAR="⚙"
SYM_CHECK="✅"
SYM_CROSS="❌"
SYM_CLOCK="⏱"
SYM_BOX="┃"

# ─── Counters ───────────────────────────────────────────────────────────────
_PASS=0
_FAIL=0
_WARN=0

# ─── Results array (for JSON mode) ─────────────────────────────────────────
declare -a _RESULTS=()

# =============================================================================
#  §1  UTILITY FUNCTIONS
# =============================================================================

# Print a section header
section() {
    local title="$1"
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  ${SYM_GEAR}  ${title}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# Print a check result
pass() {
    local msg="$1"
    echo -e "  ${GREEN}${SYM_OK}${NC}  ${msg}"
    ((_PASS++)) || true
    _RESULTS+=("PASS|${msg}")
}

fail() {
    local msg="$1"
    echo -e "  ${RED}${SYM_FAIL}${NC}  ${msg}"
    ((_FAIL++)) || true
    _RESULTS+=("FAIL|${msg}")
}

warn() {
    local msg="$1"
    echo -e "  ${YELLOW}${SYM_WARN}${NC}  ${msg}"
    ((_WARN++)) || true
    _RESULTS+=("WARN|${msg}")
}

info() {
    local msg="$1"
    echo -e "  ${DIM}${SYM_INFO}  ${msg}${NC}"
}

# Print a key-value line
kv() {
    local key="$1" val="$2"
    printf "      ${DIM}%-18s${NC} %s\n" "${key}:" "${val}"
}

# Check if a command exists
has_cmd() { command -v "$1" &>/dev/null; }

# Safe JSON reader — uses jq if available, falls back to Python
json_read() {
    local file="$1" key="$2"
    if has_cmd jq; then
        jq -r "$key // empty" "$file" 2>/dev/null
    else
        python3 -c "
import json, sys
try:
    with open('$file') as f: d = json.load(f)
    keys = '$key'.strip('.').split('.')
    for k in keys:
        if isinstance(d, dict): d = d.get(k)
        else: sys.exit(1)
    if d is not None: print(d)
except: pass
" 2>/dev/null
    fi
}

# Validate a JSON file — returns 0 if valid, 1 if corrupt
validate_json() {
    local file="$1"
    if has_cmd jq; then
        jq empty "$file" 2>/dev/null
    else
        python3 -c "import json; json.load(open('$file'))" 2>/dev/null
    fi
}

# Read a value from .env (no exports, no side effects)
env_read() {
    local key="$1" file="${2:-.env}"
    grep "^${key}=" "$file" 2>/dev/null | head -1 | cut -d'=' -f2-
}

# Human-readable bytes
human_bytes() {
    local bytes=$1
    if (( bytes >= 1073741824 )); then
        printf "%.1f GB" "$(echo "scale=1; $bytes/1073741824" | bc 2>/dev/null || echo "$((bytes/1073741824))")"
    elif (( bytes >= 1048576 )); then
        printf "%.1f MB" "$(echo "scale=1; $bytes/1048576" | bc 2>/dev/null || echo "$((bytes/1048576))")"
    else
        printf "%d B" "$bytes"
    fi
}

# Elapsed time since _SETUP_START
elapsed_ms() {
    local now end
    now=$(date +%s%N 2>/dev/null || date +%s)
    local diff=$(( (now - _SETUP_START) / 1000000 ))
    if (( diff >= 1000 )); then
        printf "%.1fs" "$(echo "scale=1; $diff/1000" | bc 2>/dev/null || echo "$((diff/1000))")"
    else
        printf "%dms" "$diff"
    fi
}

# =============================================================================
#  §2  BANNER
# =============================================================================
print_banner() {
    local git_commit git_short repo_url
    git_commit=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    git_short="${git_commit:0:7}"
    repo_url=$(git remote get-url origin 2>/dev/null || echo "local")
    # Truncate URL to fit box
    if (( ${#repo_url} > 52 )); then
        repo_url="${repo_url:0:49}..."
    fi

    local box_inner=62

    # Print one line inside the box. $1 = plain (uncolored) text used to
    # compute padding, $2 = the actual (possibly colored) text to print.
    # Keeping padding driven by the plain-text length means this never
    # goes out of alignment when dates, hashes, or URLs change length.
    #
    # NOTE: bash's ${#string} counts BYTES, not characters, under a
    # non-UTF-8 locale (e.g. LC_CTYPE=POSIX) — which silently breaks the
    # box alignment for any line containing multi-byte characters (█, ·).
    # python3 is a hard dependency of this project, so use it for a
    # locale-independent character count instead of ${#plain}.
    _box_line() {
        local plain="$1" colored="$2"
        local plain_len
        plain_len=$(printf '%s' "$plain" | python3 -c "import sys; print(len(sys.stdin.read()))")
        local pad=$(( box_inner - 2 - plain_len ))
        (( pad < 0 )) && pad=0
        printf "${MAGENTA}║${NC}  %b%*s${MAGENTA}║${NC}\n" "$colored" "$pad" ""
    }

    local date_str
    date_str="$(date '+%Y-%m-%d %H:%M %Z')"
    local subtitle="Version ${SETUP_VERSION}  ·  Commit ${git_short}  ·  ${date_str}"

    echo ""
    echo -e "${MAGENTA}╔══════════════════════════════════════════════════════════════╗${NC}"
    _box_line "" ""
    _box_line "█████ █████ █████ ████   ███  █████" "${BOLD}${WHITE}█████ █████ █████ ████   ███  █████${NC}"
    _box_line "    █ █       █   █   █ █   █   █  " "${BOLD}${WHITE}    █ █       █   █   █ █   █   █  ${NC}"
    _box_line "   █  ████    █   ████  █   █   █    AI" "${BOLD}${WHITE}   █  ████    █   ████  █   █   █  ${NC}  ${DIM}AI${NC}"
    _box_line "  █   █       █   █   █ █   █   █  " "${BOLD}${WHITE}  █   █       █   █   █ █   █   █  ${NC}"
    _box_line " █    █       █   █   █ █   █   █  " "${BOLD}${WHITE} █    █       █   █   █ █   █   █  ${NC}"
    _box_line "█████ █████   █   ████   ███    █  " "${BOLD}${WHITE}█████ █████   █   ████   ███    █  ${NC}"
    _box_line "" ""
    _box_line "Production Setup & Health Check" "${BOLD}Production Setup & Health Check${NC}"
    _box_line "$subtitle" "${DIM}${subtitle}${NC}"
    _box_line "$repo_url" "${DIM}${repo_url}${NC}"
    _box_line "" ""
    echo -e "${MAGENTA}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    unset -f _box_line
}

# =============================================================================
#  §3  SYSTEM CHECK
# =============================================================================
check_system() {
    section "System Environment"

    # ── OS ──
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        local os_name="${PRETTY_NAME:-${NAME:-Unknown} ${VERSION:-}}"
        pass "OS: ${os_name}"
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        local os_ver
        os_ver=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
        pass "OS: macOS ${os_ver}"
    else
        local os_raw
        os_raw=$(uname -srm 2>/dev/null || echo "unknown")
        warn "OS: ${os_raw} (unidentified distro)"
    fi

    # ── Architecture ──
    local arch
    arch=$(uname -m 2>/dev/null || echo "unknown")
    case "$arch" in
        x86_64|amd64)   pass "Architecture: ${arch} (x86_64)" ;;
        aarch64|arm64)   pass "Architecture: ${arch} (ARM64)" ;;
        armv7l|armhf)    pass "Architecture: ${arch} (ARM 32-bit)" ;;
        *)               warn "Architecture: ${arch} (untested)" ;;
    esac

    # ── RAM ──
    local ram_bytes
    if [[ -f /proc/meminfo ]]; then
        ram_bytes=$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0)
    else
        ram_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    fi
    if (( ram_bytes > 0 )); then
        local ram_gb=$(( ram_bytes / 1073741824 ))
        if (( ram_gb >= 2 )); then
            pass "RAM: ${ram_gb} GB"
        elif (( ram_gb >= 1 )); then
            warn "RAM: ${ram_gb} GB (recommended: 2+ GB)"
        else
            fail "RAM: ${ram_gb} GB (minimum: 1 GB)"
        fi
    else
        warn "RAM: could not detect"
    fi

    # ── Disk ──
    local disk_avail
    disk_avail=$(df -B1 . 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
    if (( disk_avail > 0 )); then
        local disk_gb=$(( disk_avail / 1073741824 ))
        if (( disk_gb >= 1 )); then
            pass "Disk: ${disk_gb} GB available"
        else
            fail "Disk: ${disk_gb} GB available (minimum: 1 GB)"
        fi
    else
        warn "Disk: could not detect available space"
    fi

    # ── Internet ──
    if has_cmd curl; then
        if curl -sSf --connect-timeout 5 --max-time 10 \
            "https://api.binance.com/api/v3/ping" &>/dev/null; then
            pass "Internet: connected (Binance API reachable)"
        elif curl -sSf --connect-timeout 5 --max-time 10 \
            "https://httpbin.org/get" &>/dev/null; then
            pass "Internet: connected"
        else
            warn "Internet: no external connectivity detected"
        fi
    elif has_cmd wget; then
        if wget -q --spider --timeout=10 "https://httpbin.org/get" 2>/dev/null; then
            pass "Internet: connected (via wget)"
        else
            warn "Internet: connectivity check failed"
        fi
    else
        warn "Internet: cannot check (no curl or wget)"
    fi
}

# =============================================================================
#  §4  PYTHON CHECK
# =============================================================================
check_python() {
    section "Python Runtime"

    # ── Python binary ──
    local py_cmd=""
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if has_cmd "$candidate"; then
            py_cmd="$candidate"
            break
        fi
    done

    if [[ -z "$py_cmd" ]]; then
        fail "Python 3 not found — install python3 (3.10+)"
        return 1
    fi

    local py_version
    py_version=$("$py_cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    local py_major py_minor
    py_major=$("$py_cmd" -c "import sys; print(sys.version_info.major)")
    py_minor=$("$py_cmd" -c "import sys; print(sys.version_info.minor)")

    if (( py_major >= 3 && py_minor >= 10 )); then
        pass "Python: ${py_version} (${py_cmd})"
    elif (( py_major >= 3 && py_minor >= 3 )); then
        warn "Python: ${py_version} — recommended 3.10+"
    else
        fail "Python: ${py_version} — requires 3.10+"
        return 1
    fi

    # ── pip ──
    if "$py_cmd" -m pip --version &>/dev/null; then
        local pip_ver
        pip_ver=$("$py_cmd" -m pip --version 2>/dev/null | awk '{print $2}')
        pass "pip: ${pip_ver}"
    else
        warn "pip: not available — run: ${py_cmd} -m ensurepip"
    fi

    # ── Virtual environment ──
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        pass "Virtual env: active (${VIRTUAL_ENV})"
    elif [[ -d ".venv" ]]; then
        pass "Virtual env: .venv/ exists (activate with: source .venv/bin/activate)"
    else
        info "No virtual environment detected"
        info "Create one: python3 -m venv .venv && source .venv/bin/activate"
    fi

    # Store for later use
    _PYTHON="$py_cmd"
}

# =============================================================================
#  §5  DEPENDENCIES
# =============================================================================
check_dependencies() {
    section "Dependencies"

    local req_file="requirements.txt"

    if [[ ! -f "$req_file" ]]; then
        fail "requirements.txt not found"
        return 1
    fi

    local total_pkgs missing_pkgs installed_pkgs
    total_pkgs=$(grep -cv '^\s*$\|^\s*#' "$req_file" 2>/dev/null || echo 0)
    missing_pkgs=0
    installed_pkgs=0

    info "Checking ${total_pkgs} packages from requirements.txt ..."

    # Build a single Python check for all packages (one process, fast)
    local pkg_names=""
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        local pkg_name
        pkg_name=$(echo "$line" | sed 's/[>=<!\[].*//' | xargs)
        [[ -n "$pkg_name" ]] && pkg_names="${pkg_names} ${pkg_name//-/_}"
    done < "$req_file"

    local installed_list
    installed_list=$("$_PYTHON" -c "
import importlib, sys
names = '${pkg_names}'.split()
for n in names:
    try:
        importlib.import_module(n)
        print(f'OK {n}')
    except ImportError:
        print(f'MISS {n}')
" 2>/dev/null || echo "")

    installed_pkgs=$(echo "$installed_list" | grep -c "^OK" || echo 0)
    missing_pkgs=$(echo "$installed_list" | grep -c "^MISS" || echo 0)

    if (( missing_pkgs == 0 )); then
        pass "All ${installed_pkgs} packages installed"
    else
        warn "${missing_pkgs} package(s) missing out of ${total_pkgs}"

        if [[ "$AUTO_MODE" == "true" ]]; then
            info "Installing missing packages (--auto mode) ..."
            "$_PYTHON" -m pip install -r "$req_file" --quiet 2>/dev/null && \
                pass "Dependencies installed" || \
                warn "Some packages failed to install"
        else
            info "Run: pip install -r ${req_file}"
        fi
    fi
}

# =============================================================================
#  §6  CONFIGURATION (.env)
# =============================================================================
check_configuration() {
    section "Configuration"

    local env_file=".env"
    local env_example=".env.example"

    # ── .env existence ──
    if [[ ! -f "$env_file" ]]; then
        if [[ -f "$env_example" ]]; then
            if [[ "$AUTO_MODE" == "true" ]]; then
                cp "$env_example" "$env_file"
                chmod 600 "$env_file" 2>/dev/null || true
                pass "Created ${env_file} from ${env_example}"
            else
                echo ""
                echo -e "  ${YELLOW}${SYM_WARN}  No .env file found.${NC}"
                echo -e "  ${DIM}A template exists at ${env_example}${NC}"
                echo ""
                read -rp "  Create .env from template? [Y/n] " answer
                answer="${answer:-Y}"
                if [[ "$answer" =~ ^[Yy] ]]; then
                    cp "$env_example" "$env_file"
                    chmod 600 "$env_file" 2>/dev/null || true
                    pass "Created ${env_file} from ${env_example}"
                    echo -e "  ${YELLOW}${SYM_WARN}  Edit ${env_file} with your credentials before running:${NC}"
                    echo -e "  ${BOLD}nano ${env_file}${NC}"
                else
                    fail "Cannot proceed without .env"
                    return 1
                fi
            fi
        else
            fail "Neither .env nor .env.example found"
            return 1
        fi
    else
        pass ".env file present"
    fi

    # ── Missing keys vs template (stale .env from before an option was added) ──
    if [[ -f "$env_example" ]]; then
        local missing_keys=()
        while IFS= read -r key; do
            grep -q "^${key}=" "$env_file" 2>/dev/null || missing_keys+=("$key")
        done < <(grep -oE '^[A-Z_0-9]+=' "$env_example" | sed 's/=$//')

        if (( ${#missing_keys[@]} > 0 )); then
            warn "${#missing_keys[@]} key(s) in ${env_example} are missing from ${env_file}"
            info "Missing: ${missing_keys[*]}"
            info "Add them manually, or diff against ${env_example}"
        fi
    fi

    # ── Required variables ──
    # API_KEY/API_SECRET are only required for live trading (PAPER_MODE=false).
    # TELEGRAM_TOKEN/TELEGRAM_CHAT_ID are only required when Telegram is on.
    local paper_mode telegram_enabled
    paper_mode=$(env_read "PAPER_MODE" "$env_file")
    telegram_enabled=$(env_read "TELEGRAM_ENABLED" "$env_file")

    local required_vars=("EXCHANGE")
    if [[ "$paper_mode" == "false" ]]; then
        required_vars+=("API_KEY" "API_SECRET")
    fi
    if [[ "$telegram_enabled" == "true" ]]; then
        required_vars+=("TELEGRAM_TOKEN" "TELEGRAM_CHAT_ID")
    fi

    local missing=0
    for var in "${required_vars[@]}"; do
        local val
        val=$(env_read "$var" "$env_file")
        if [[ -z "$val" ]]; then
            fail "Required variable ${var} is empty"
            ((missing++)) || true
        fi
    done

    if (( missing == 0 )); then
        pass "All required variables set"
    else
        warn "${missing} required variable(s) empty — edit .env"
    fi

    if [[ -z "$paper_mode" ]]; then
        info "PAPER_MODE not set — defaults to true (paper trading, safe)"
    fi

    # ── Validate numeric ranges ──
    _check_positive() {
        local label="$1" val="$2"
        if [[ -n "$val" ]]; then
            "$_PYTHON" -c "import sys; sys.exit(0 if float('${val}') > 0 else 1)" 2>/dev/null || \
                warn "${label}=${val} — should be > 0"
        fi
    }
    _check_positive "ACCOUNT_BALANCE"        "$(env_read ACCOUNT_BALANCE "$env_file")"
    _check_positive "MAX_RISK_PER_TRADE_PCT" "$(env_read MAX_RISK_PER_TRADE_PCT "$env_file")"
    _check_positive "MIN_RR"                 "$(env_read MIN_RR "$env_file")"
    _check_positive "MAX_RR"                 "$(env_read MAX_RR "$env_file")"

    local pi
    pi=$(env_read "PIPELINE_INTERVAL" "$env_file")
    if [[ -n "$pi" ]] && [[ "$pi" =~ ^[0-9]+$ ]] && (( pi < 10 )); then
        warn "PIPELINE_INTERVAL=${pi} — very aggressive (< 10s)"
    fi
}

# =============================================================================
#  §7  EXCHANGE
# =============================================================================
check_exchange() {
    section "Exchange Configuration"

    local exchange api_key api_secret mode

    exchange=$(env_read "EXCHANGE" ".env")
    api_key=$(env_read "API_KEY" ".env")
    api_secret=$(env_read "API_SECRET" ".env")
    mode=$(env_read "PAPER_MODE" ".env")

    # ── Exchange name ──
    if [[ -n "$exchange" ]]; then
        case "$exchange" in
            binance|bybit|okx|kucoin|coinbase|kraken|gate|huobi|bitget|mexc|phemex)
                pass "Exchange: ${exchange}"
                ;;
            *)
                warn "Exchange: '${exchange}' — untested, may not work"
                ;;
        esac
    else
        fail "Exchange: not configured"
        return 1
    fi

    # ── API credentials ──
    if [[ -n "$api_key" && -n "$api_secret" ]]; then
        # Mask the key for display
        local masked_key="${api_key:0:6}...${api_key: -4}"
        pass "API Key: ${masked_key}"
    elif [[ -n "$api_key" ]]; then
        warn "API Key set but API_SECRET is empty"
    elif [[ -n "$api_secret" ]]; then
        warn "API_SECRET set but API_KEY is empty"
    else
        info "API credentials not set — paper trading only"
    fi

    # ── Paper / Live mode ──
    # AppConfig defaults PAPER_MODE to true when unset — mirror that here
    # instead of treating an empty value as LIVE.
    if [[ "$mode" != "false" ]]; then
        pass "Mode: PAPER (testing)"
        echo -e "  ${DIM}${SYM_INFO}  All trades are simulated — no real funds at risk${NC}"
    else
        pass "Mode: LIVE"
        echo -e "  ${YELLOW}${SYM_WARN}  Real money at risk — ensure risk limits are configured${NC}"
    fi
}

# =============================================================================
#  §8  TELEGRAM
# =============================================================================
check_telegram() {
    section "Telegram"

    local tg_enabled tg_token tg_chat

    tg_enabled=$(env_read "TELEGRAM_ENABLED" ".env")
    tg_token=$(env_read "TELEGRAM_TOKEN" ".env")
    tg_chat=$(env_read "TELEGRAM_CHAT_ID" ".env")

    # ── Enabled / Disabled ──
    if [[ "$tg_enabled" == "true" ]]; then
        pass "Telegram: enabled"
    elif [[ "$tg_enabled" == "false" ]]; then
        info "Telegram: disabled"
        return 0
    else
        warn "TELEGRAM_ENABLED='${tg_enabled}' — expected true or false"
        return 0
    fi

    # ── Token validation ──
    if [[ -z "$tg_token" || "$tg_token" == "YOUR_TELEGRAM_BOT_TOKEN" ]]; then
        fail "TELEGRAM_TOKEN not configured"
    elif [[ "$tg_token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
        local masked_token="${tg_token:0:10}...${tg_token: -5}"
        pass "Token: ${masked_token}"
    else
        warn "Token format looks unusual — expected: <digits>:<alphanumeric>"
    fi

    # ── Chat ID validation ──
    if [[ -z "$tg_chat" || "$tg_chat" == "YOUR_CHAT_ID" ]]; then
        fail "TELEGRAM_CHAT_ID not configured"
    elif [[ "$tg_chat" =~ ^-?[0-9]+$ ]]; then
        pass "Chat ID: ${tg_chat}"
    else
        warn "Chat ID format unusual — expected numeric (e.g., -1001234567890)"
    fi
}

# =============================================================================
#  §9  RUNTIME DIRECTORIES & STATE
# =============================================================================
check_runtime() {
    section "Runtime"

    # ── Directories ──
    local dirs=("data" "logs" "backups")
    for d in "${dirs[@]}"; do
        if [[ -d "$d" ]]; then
            pass "Directory: ${d}/"
        else
            mkdir -p "$d"
            pass "Directory: ${d}/ (created)"
        fi
    done

    # ── Critical JSON files ──
    local json_files=(
        "data/paper_state.json"
        "data/positions.json"
        "data/paper_balance.json"
        "data/paper_orders.json"
        "data/trade_plan.json"
        "data/state.json"
    )

    local json_ok=0
    local json_missing=0
    local json_corrupt=0

    for f in "${json_files[@]}"; do
        if [[ ! -f "$f" ]]; then
            ((json_missing++)) || true
            continue
        fi

        if validate_json "$f" 2>/dev/null; then
            ((json_ok++)) || true
        else
            ((json_corrupt++)) || true
            fail "Corrupt: ${f}"
        fi
    done

    if (( json_ok > 0 )); then
        pass "State files: ${json_ok} valid"
    fi
    if (( json_missing > 0 )); then
        info "State files: ${json_missing} missing (will be created on first run)"
    fi

    # ── PID file check ──
    local pid_file="data/zetbot.pid"
    if [[ -f "$pid_file" ]]; then
        local old_pid
        old_pid=$(cat "$pid_file" 2>/dev/null)
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
            warn "Bot already running (PID ${old_pid}) — stop it first"
        else
            info "Stale PID file (PID ${old_pid} not running) — will be replaced"
            rm -f "$pid_file"
        fi
    else
        pass "No stale PID file"
    fi

    # ── Log rotation info ──
    local log_count
    log_count=$(ls logs/*.log 2>/dev/null | wc -l || echo 0)
    if (( log_count > 50 )); then
        info "Logs: ${log_count} log files — consider cleanup"
    fi
}

# =============================================================================
#  §10  GIT STATUS
# =============================================================================
check_git() {
    section "Git"

    if ! has_cmd git; then
        warn "Git not installed — version tracking unavailable"
        return 0
    fi

    if [[ ! -d ".git" ]]; then
        info "Not a git repository"
        return 0
    fi

    # ── Branch ──
    local branch
    branch=$(git branch --show-current 2>/dev/null || echo "detached")
    pass "Branch: ${branch}"

    # ── Commit ──
    local commit
    commit=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    pass "Commit: ${commit}"

    # ── Remote ──
    local remote_url
    remote_url=$(git remote get-url origin 2>/dev/null || echo "none")
    if [[ "$remote_url" != "none" ]]; then
        pass "Remote: ${remote_url}"
    else
        info "No remote configured"
    fi

    # ── Working tree status ──
    local changes
    changes=$(git status --porcelain 2>/dev/null | wc -l || echo 0)
    if (( changes == 0 )); then
        pass "Working tree: clean"
    else
        warn "Working tree: ${changes} uncommitted change(s)"
    fi

    # ── Behind remote ──
    if [[ "$remote_url" != "none" ]]; then
        local behind
        behind=$(git rev-list --count "HEAD..@{u}" 2>/dev/null || echo "?")
        if [[ "$behind" =~ ^[0-9]+$ ]] && (( behind > 0 )); then
            warn "Behind remote by ${behind} commit(s)"
        fi
    fi
}

# =============================================================================
#  §11  OPTIONAL TOOLS
# =============================================================================
check_optional_tools() {
    section "Optional Tools"

    if has_cmd jq; then
        pass "jq: available"
    else
        info "jq: not installed (using Python fallback for JSON parsing)"
    fi

    if has_cmd curl; then
        pass "curl: available"
    else
        warn "curl: not installed — health checks and updates may fail"
    fi

    if has_cmd git; then
        pass "git: available"
    else
        info "git: not installed (optional for runtime)"
    fi

    if has_cmd bc; then
        pass "bc: available"
    else
        info "bc: not installed (using Python for calculations)"
    fi
}

# =============================================================================
#  §12  SUMMARY
# =============================================================================
print_summary() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  📋  SETUP REPORT${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Count statuses per section
    local total=$(( _PASS + _FAIL + _WARN ))

    printf "      ${GREEN}${SYM_OK} Passed${NC}   : %d\n" "$_PASS"
    printf "      ${RED}${SYM_FAIL} Failed${NC}   : %d\n" "$_FAIL"
    printf "      ${YELLOW}${SYM_WARN} Warnings${NC} : %d\n" "$_WARN"
    printf "      ${DIM}Total${NC}      : %d\n" "$total"

    echo ""

    if (( _FAIL == 0 )); then
        echo -e "      ${GREEN}${BOLD}${SYM_OK} ALL SYSTEMS OPERATIONAL${NC}"
        echo ""
        echo -e "      ${DIM}Run the bot with:${NC}"
        echo -e "      ${BOLD}python main.py${NC}"
    elif (( _FAIL <= 2 )); then
        echo -e "      ${YELLOW}${BOLD}${SYM_WARN} SETUP COMPLETE WITH WARNINGS${NC}"
        echo ""
        echo -e "      ${DIM}Review the failures above before running.${NC}"
    else
        echo -e "      ${RED}${BOLD}${SYM_FAIL} SETUP FAILED — ACTION REQUIRED${NC}"
        echo ""
        echo -e "      ${DIM}Fix the ${RED}${_FAIL}${DIM} issue(s) above and re-run:${NC}"
        echo -e "      ${BOLD}bash setup.sh${NC}"
    fi

    echo ""
    echo -e "${DIM}  Elapsed: $(elapsed_ms)${NC}"
    echo ""
}

# =============================================================================
#  §13  JSON OUTPUT
# =============================================================================
print_json() {
    echo "{"
    echo "  \"setup_version\": \"${SETUP_VERSION}\","
    echo "  \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    echo "  \"results\": ["
    local first=true
    for r in "${_RESULTS[@]}"; do
        local status="${r%%|*}"
        local msg="${r#*|}"
        if [[ "$first" == "true" ]]; then
            first=false
        else
            echo ","
        fi
        printf '    {"status": "%s", "message": "%s"}' "$status" "$msg"
    done
    echo ""
    echo "  ],"
    echo "  \"summary\": {"
    echo "    \"passed\": ${_PASS},"
    echo "    \"failed\": ${_FAIL},"
    echo "    \"warnings\": ${_WARN},"
    echo "    \"operational\": $([ "$_FAIL" -eq 0 ] && echo "true" || echo "false")"
    echo "  }"
    echo "}"
}

# =============================================================================
#  §14  MAIN
# =============================================================================
main() {
    if [[ "$JSON_MODE" != "true" ]]; then
        print_banner
    fi

    if [[ "$JSON_MODE" == "true" ]]; then
        # In JSON mode, suppress all visual output — redirect to /dev/null
        _REAL_STDOUT=1
        exec 3>&1 4>&2
        exec >/dev/null 2>&1
    fi

    check_system           || true
    check_python           || true
    check_dependencies     || true
    check_configuration    || true
    check_exchange         || true
    check_telegram         || true
    check_runtime          || true
    check_git              || true
    check_optional_tools   || true

    if [[ "$JSON_MODE" == "true" ]]; then
        # Restore original stdout/stderr for JSON output
        exec 1>&3 2>&4
        print_json
    else
        print_summary
    fi

    # Exit code: 0 if no failures, 1 otherwise
    if (( _FAIL > 0 )); then
        exit 1
    fi
    exit 0
}

main "$@"
