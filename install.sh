#!/usr/bin/env bash
set -euo pipefail

if [ -t 1 ]; then
  BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
else
  BOLD=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi

step() { printf '\n%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok() { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
case "$SCRIPT_PATH" in
  */*)
    if SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd -P)"; then
      :
    else
      SCRIPT_DIR="$(pwd -P)"
    fi
    ;;
  *) SCRIPT_DIR="$(pwd -P)" ;;
esac

INSTALL_DIR="${MAXWELL_INSTALL_DIR:-$HOME/maxwell}"
REPO_URL="${MAXWELL_REPO_URL:-https://github.com/Z3ki/Maxwell-bot.git}"
BRANCH="${MAXWELL_BRANCH:-main}"
RECONFIGURE=0
LOCAL_MODE=0
NO_EXTRAS=0
NONINTERACTIVE="${MAXWELL_NONINTERACTIVE:-0}"
SKIP_SYSTEM_DEPS="${MAXWELL_SKIP_SYSTEM_DEPS:-0}"
TTY=""
OS_FAMILY=""
PYTHON_BIN="python3"

usage() {
  cat <<'EOF'
Maxwell installer

Usage:
  bash install.sh [options]

Options:
  --help              Show this help.
  --reconfigure       Run the configuration wizard even when .env exists.
  --no-extras         Do not install optional Python/system extras.
  --non-interactive   Read all answers from environment variables.
  --dir <path>        Install/update Maxwell in this directory.
  --local             Configure the current checkout instead of cloning/updating.

Useful environment variables:
  MAXWELL_INSTALL_DIR, MAXWELL_REPO_URL, MAXWELL_BRANCH,
  MAXWELL_NONINTERACTIVE=1, MAXWELL_SKIP_SYSTEM_DEPS=1,
  DISCORD_TOKEN, OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY,
  MAXWELL_OWNER_IDS, MAXWELL_ADMIN_PASSWORD,
  MAXWELL_INSTALL_EXTRAS=yes|no, MAXWELL_INSTALL_DOCKER=yes|no
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --no-extras) NO_EXTRAS=1; MAXWELL_INSTALL_EXTRAS=no ;;
    --non-interactive) NONINTERACTIVE=1 ;;
    --dir) shift; [ "$#" -gt 0 ] || fail "--dir requires a path"; INSTALL_DIR="$1" ;;
    --local) LOCAL_MODE=1; INSTALL_DIR="$SCRIPT_DIR"; SKIP_SYSTEM_DEPS="${MAXWELL_SKIP_SYSTEM_DEPS:-1}" ;;
    *) fail "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ -r /dev/tty ] && [ -w /dev/tty ]; then
  TTY=/dev/tty
elif [ "$NONINTERACTIVE" != "1" ]; then
  NONINTERACTIVE=1
  warn "No controlling TTY is available; switching to non-interactive mode."
  warn "Set DISCORD_TOKEN, OLLAMA_MODEL, and other MAXWELL_* variables, then re-run with --reconfigure if needed."
fi

prompt() {
  prompt_text=$1
  default_value=${2:-}
  answer=""
  if [ "$NONINTERACTIVE" = "1" ]; then
    printf '%s' "$default_value"
    return 0
  fi
  if [ -n "$default_value" ]; then
    printf '  %s [%s]: ' "$prompt_text" "$default_value" > "$TTY"
  else
    printf '  %s: ' "$prompt_text" > "$TTY"
  fi
  IFS= read -r answer < "$TTY" || answer=""
  if [ -n "$answer" ]; then printf '%s' "$answer"; else printf '%s' "$default_value"; fi
}

prompt_secret() {
  prompt_text=$1
  default_value=${2:-}
  answer=""
  if [ "$NONINTERACTIVE" = "1" ]; then
    printf '%s' "$default_value"
    return 0
  fi
  if [ -n "$default_value" ]; then
    printf '  %s [press Enter to keep existing/default]: ' "$prompt_text" > "$TTY"
  else
    printf '  %s: ' "$prompt_text" > "$TTY"
  fi
  IFS= read -r -s answer < "$TTY" || answer=""
  printf '\n' > "$TTY"
  if [ -n "$answer" ]; then printf '%s' "$answer"; else printf '%s' "$default_value"; fi
}

yes_no() {
  prompt_text=$1
  default_value=${2:-no}
  env_value=${3:-}
  if [ -n "$env_value" ]; then
    case "$env_value" in yes|YES|Yes|y|Y|1|true|TRUE|on|ON) printf 'yes'; return 0 ;; no|NO|No|n|N|0|false|FALSE|off|OFF) printf 'no'; return 0 ;; esac
  fi
  if [ "$NONINTERACTIVE" = "1" ]; then
    printf '%s' "$default_value"
    return 0
  fi
  while :; do
    answer=$(prompt "$prompt_text" "$default_value")
    case "$answer" in yes|YES|Yes|y|Y) printf 'yes'; return 0 ;; no|NO|No|n|N) printf 'no'; return 0 ;; *) warn "Please answer yes or no." ;; esac
  done
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    if ! command -v sudo >/dev/null 2>&1; then
      fail "sudo is required to install system packages as a non-root user. Install the packages manually or set MAXWELL_SKIP_SYSTEM_DEPS=1."
    fi
    warn "Using sudo to install system packages needed by Maxwell. You may be prompted for your password."
    sudo "$@"
  fi
}

detect_os() {
  if command -v apt-get >/dev/null 2>&1; then OS_FAMILY=apt; return; fi
  if command -v dnf >/dev/null 2>&1; then OS_FAMILY=dnf; return; fi
  if command -v pacman >/dev/null 2>&1; then OS_FAMILY=pacman; return; fi
  if [ "$(uname -s 2>/dev/null || printf unknown)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then OS_FAMILY=brew; return; fi
    fail "macOS detected but Homebrew is missing. Install Homebrew plus: git curl python@3.11 (or newer)."
  fi
  cat >&2 <<EOF
Unsupported OS/package manager.
Install these manually, then re-run with MAXWELL_SKIP_SYSTEM_DEPS=1:
  Required: git curl Python 3.11+ with venv and pip
  Optional extras: ffmpeg, libopus/opus, libsodium, espeak-ng, nodejs
  Optional shell tool: Docker Engine with a daemon reachable by your user
EOF
  exit 1
}

install_core_system_deps() {
  [ "$SKIP_SYSTEM_DEPS" = "1" ] && { warn "Skipping system package installation (MAXWELL_SKIP_SYSTEM_DEPS=1)."; return; }
  detect_os
  step "Installing required system packages"
  case "$OS_FAMILY" in
    apt) run_as_root apt-get update; run_as_root apt-get install -y git curl ca-certificates python3 python3-venv python3-pip ;;
    dnf) run_as_root dnf install -y git curl ca-certificates python3 python3-pip ;;
    pacman) run_as_root pacman -Sy --needed --noconfirm git curl ca-certificates python python-pip ;;
    brew) brew install git curl python ;;
  esac
}

install_extra_system_deps() {
  [ "$SKIP_SYSTEM_DEPS" = "1" ] && { warn "Skipping optional system packages (MAXWELL_SKIP_SYSTEM_DEPS=1)."; return; }
  [ -n "$OS_FAMILY" ] || detect_os
  step "Installing optional system packages"
  case "$OS_FAMILY" in
    apt) run_as_root apt-get update; run_as_root apt-get install -y ffmpeg libopus0 libsodium-dev espeak-ng nodejs ;;
    dnf) run_as_root dnf install -y ffmpeg opus libsodium-devel espeak-ng nodejs ;;
    pacman) run_as_root pacman -Sy --needed --noconfirm ffmpeg opus libsodium espeak-ng nodejs ;;
    brew) brew install ffmpeg opus libsodium espeak-ng node ;;
  esac
}

verify_python() {
  command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install Python 3.11+ with venv and pip."
  if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  then
    fail "Python 3.11+ required; found $(python3 -V 2>&1)."
  fi
  PYTHON_BIN=python3
  ok "$(python3 -V)"
}

clone_or_update() {
  step "Getting Maxwell"
  if [ "$LOCAL_MODE" = "1" ]; then
    [ -f "$INSTALL_DIR/bot.py" ] || fail "--local must be run from a Maxwell checkout."
    cd "$INSTALL_DIR"
    ok "using local checkout at $INSTALL_DIR"
    return
  fi
  if [ ! -e "$INSTALL_DIR" ]; then
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "cloned $REPO_URL ($BRANCH) to $INSTALL_DIR"
  elif [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    if git pull --ff-only; then
      ok "updated existing checkout"
    else
      warn "git pull --ff-only failed; continuing without overwriting local changes."
    fi
  else
    fail "$INSTALL_DIR exists but is not a git repository. Move it aside or choose --dir <path>."
  fi
  cd "$INSTALL_DIR"
}

set_env_value() {
  "$PYTHON_BIN" scripts/set_env.py .env "$1" "$2"
}

copy_env_if_needed() {
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    ok "created .env from .env.example"
  fi
}

install_python_deps() {
  step "Installing Python dependencies"
  if [ ! -d .venv ]; then
    "$PYTHON_BIN" -m venv .venv || fail "Could not create .venv. On Debian/Ubuntu, install python3-venv."
    ok "created .venv"
  fi
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  ok "core Python dependencies installed"

  printf '  Optional extras unlock: web search (ddgs), YouTube (yt-dlp), voice/VC (PyNaCl + opus), TTS (gTTS/espeak), and video/audio helpers (ffmpeg/node).\n'
  extras_default=no
  [ "$NO_EXTRAS" = "1" ] && extras_default=no
  extras=$(yes_no "Install optional extras too?" "$extras_default" "${MAXWELL_INSTALL_EXTRAS:-}")
  if [ "$extras" = "yes" ]; then
    install_extra_system_deps
    python -m pip install --quiet -r requirements-optional.txt
    ok "optional Python extras installed"
  else
    ok "optional extras skipped"
  fi
}

docker_reachable() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

install_docker_if_requested() {
  if docker_reachable; then
    ok "Docker daemon reachable"
    return 0
  fi
  warn "The shell tool runs commands inside Docker, but Docker is absent or unreachable."
  docker_choice=$(yes_no "Install/enable Docker for the shell tool?" "no" "${MAXWELL_INSTALL_DOCKER:-}")
  if [ "$docker_choice" != "yes" ]; then
    set_env_value ENABLE_SHELL false
    warn "Set ENABLE_SHELL=false in .env. Re-enable it after Docker works."
    return 0
  fi
  [ "$SKIP_SYSTEM_DEPS" = "1" ] && { warn "Cannot install Docker while MAXWELL_SKIP_SYSTEM_DEPS=1; disabling shell."; set_env_value ENABLE_SHELL false; return 0; }
  [ -n "$OS_FAMILY" ] || detect_os
  step "Installing Docker"
  case "$OS_FAMILY" in
    apt|dnf)
      curl -fsSL https://get.docker.com | run_as_root sh
      ;;
    pacman)
      run_as_root pacman -Sy --needed --noconfirm docker
      run_as_root systemctl enable --now docker || true
      ;;
    brew)
      warn "Installing Docker Desktop with Homebrew. Start Docker Desktop after installation."
      brew install --cask docker
      ;;
  esac
  if command -v docker >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ] && getent group docker >/dev/null 2>&1; then
    run_as_root usermod -aG docker "$USER" || true
    warn "Added $USER to the docker group. Log out and back in before using Docker without sudo."
  fi
  if docker_reachable; then
    set_env_value ENABLE_SHELL true
    ok "Docker is reachable; shell tool remains enabled"
  else
    set_env_value ENABLE_SHELL false
    warn "Docker is still not reachable; set ENABLE_SHELL=false. Re-run --reconfigure after fixing Docker."
  fi
}

configure_env() {
  step "Configuring Maxwell"
  if [ -f .env ] && [ "$RECONFIGURE" != "1" ]; then
    ok ".env already exists — leaving it unchanged (use --reconfigure to edit it)"
    return
  fi
  if [ -f .env ] && [ "$RECONFIGURE" = "1" ] && [ "$NONINTERACTIVE" != "1" ]; then
    keep=$(yes_no ".env exists. Update it with the wizard?" "yes" "")
    [ "$keep" = "yes" ] || { ok "kept existing .env"; return; }
  fi
  copy_env_if_needed

  printf '\n%sStep 1/5: Discord user token%s\n' "$BOLD" "$RESET"
  printf '  This is a self-bot user token. In a browser, open Discord, DevTools, Network, select a discord.com/api request, and copy the authorization header. You can also inspect Application/Local Storage. This may violate Discord ToS.\n'
  token=$(prompt_secret "Discord token (blank to skip)" "${DISCORD_TOKEN:-}")
  if [ -n "$token" ]; then set_env_value DISCORD_TOKEN "$token"; ok "Discord token saved"; else warn "DISCORD_TOKEN left blank; the bot cannot start until you edit .env."; fi

  printf '\n%sStep 2/5: LLM provider%s\n' "$BOLD" "$RESET"
  base_default="${OLLAMA_BASE_URL:-http://localhost:11434}"
  model_default="${OLLAMA_MODEL:-qwen3:8b}"
  api_key_default="${OLLAMA_API_KEY:-}"
  if [ "$NONINTERACTIVE" != "1" ] && [ -z "${OLLAMA_BASE_URL:-}" ] && [ -z "${OLLAMA_MODEL:-}" ]; then
    printf '  Choose an OpenAI-compatible provider:\n' > "$TTY"
    printf '    1) Local Ollama (http://localhost:11434)\n    2) OpenRouter (https://openrouter.ai/api/v1, key from openrouter.ai/keys, free model moonshotai/kimi-k2.6:free)\n    3) OpenAI (https://api.openai.com/v1)\n    4) LM Studio (http://localhost:1234/v1)\n    5) Custom OpenAI-compatible URL\n' > "$TTY"
    provider=$(prompt "Provider" "1")
    case "$provider" in
      1) base_default=http://localhost:11434; model_default=qwen3:8b; api_key_default="" ;;
      2) base_default=https://openrouter.ai/api/v1; model_default=moonshotai/kimi-k2.6:free ;;
      3) base_default=https://api.openai.com/v1; model_default=gpt-4.1-mini ;;
      4) base_default=http://localhost:1234/v1; model_default="local-model"; api_key_default="" ;;
      5) base_default=$(prompt "Custom base URL" "$base_default"); model_default="" ;;
      *) warn "Unknown choice; using Local Ollama defaults." ;;
    esac
    if [ "$provider" = "1" ]; then
      ollama_choice=$(yes_no "Install Ollama and pull the selected model?" "no" "")
      if [ "$ollama_choice" = "yes" ]; then
        if [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ]; then
          curl -fsSL https://ollama.com/install.sh | sh
          if command -v ollama >/dev/null 2>&1; then
            ollama pull "$model_default" || true
            ollama pull qwen3-embedding:0.6b || true
          fi
        else
          warn "Install Ollama from https://ollama.com/download, then run: ollama pull $model_default"
        fi
      fi
    fi
  fi
  base=$(prompt "Provider base URL" "$base_default")
  model=$(prompt "Model name" "$model_default")
  key=$(prompt_secret "API key (blank for local providers)" "$api_key_default")
  set_env_value OLLAMA_BASE_URL "$base"
  if [ -n "$model" ]; then set_env_value OLLAMA_MODEL "$model"; else warn "OLLAMA_MODEL left blank; set it before starting Maxwell."; fi
  set_env_value OLLAMA_API_KEY "$key"

  printf '\n%sStep 3/5: Owner Discord user ID(s)%s\n' "$BOLD" "$RESET"
  printf '  Enable Discord Developer Mode, right-click yourself, and choose Copy User ID. Use commas for multiple owners.\n'
  owner=$(prompt "Owner ID(s), optional" "${MAXWELL_OWNER_IDS:-}")
  if [ -n "$owner" ]; then
    set_env_value MAXWELL_OWNER_IDS "$owner"
  else
    warn "MAXWELL_OWNER_IDS left blank; admin commands will be denied."
  fi

  printf '\n%sStep 4/5: Dashboard password%s\n' "$BOLD" "$RESET"
  printf '  Empty MAXWELL_ADMIN_PASSWORD makes the dashboard/admin API answer 503. Press Enter interactively to generate one.\n'
  admin_pw_default="${MAXWELL_ADMIN_PASSWORD:-}"
  admin_pw=$(prompt_secret "Dashboard admin password" "$admin_pw_default")
  if [ -z "$admin_pw" ] && [ "$NONINTERACTIVE" != "1" ]; then
    if command -v openssl >/dev/null 2>&1; then admin_pw=$(openssl rand -hex 16); else admin_pw=$(python3 -c 'import secrets; print(secrets.token_hex(16))'); fi
    printf '  Generated dashboard password: %s\n' "$admin_pw"
  fi
  if [ -n "$admin_pw" ]; then
    set_env_value MAXWELL_ADMIN_PASSWORD "$admin_pw"
  else
    warn "MAXWELL_ADMIN_PASSWORD left blank; dashboard/admin API will answer 503."
  fi

  printf '\n%sStep 5/5: Optional background loops%s\n' "$BOLD" "$RESET"
  printf '  Autonomy and REM spend LLM tokens on timers, so the safe default is off.\n'
  autonomy=$(yes_no "Enable autonomy background actions?" "no" "${ENABLE_AUTONOMY:-}")
  rem=$(yes_no "Enable REM memory consolidation?" "no" "${ENABLE_REM:-}")
  if [ "$autonomy" = "yes" ]; then
    set_env_value ENABLE_AUTONOMY true
  else
    set_env_value ENABLE_AUTONOMY false
  fi
  if [ "$rem" = "yes" ]; then
    set_env_value ENABLE_REM true
  else
    set_env_value ENABLE_REM false
  fi

  install_docker_if_requested
}

write_run_script() {
  cat > run.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
. .venv/bin/activate
exec python3 bot.py "$@"
EOF
  chmod +x run.sh
  ok "wrote run.sh"
}

offer_systemd() {
  [ "$NONINTERACTIVE" = "1" ] && return 0
  [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ] || return 0
  choice=$(yes_no "Create a systemd user service for Maxwell?" "no" "")
  [ "$choice" = "yes" ] || return 0
  mkdir -p "$HOME/.config/systemd/user"
  service="$HOME/.config/systemd/user/maxwell.service"
  cat > "$service" <<EOF
[Unit]
Description=Maxwell Discord self-bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd -P)
ExecStart=$(pwd -P)/.venv/bin/python3 bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload || true
  ok "wrote $service"
  printf '  Enable it with: systemctl --user enable --now maxwell\n'
}

run_doctor() {
  step "Verifying installation"
  # shellcheck disable=SC1091
  . .venv/bin/activate
  if python3 doctor.py; then
    ok "doctor.py reports the install is ready to start"
  else
    warn "doctor.py found startup blockers. Fix the items above, then run python3 doctor.py again."
  fi
  if grep -q '^DISCORD_TOKEN=.' .env && grep -q '^OLLAMA_MODEL=.' .env; then
    if python3 doctor.py --probe; then
      ok "live endpoint probe succeeded"
    else
      warn "doctor.py --probe failed. Check docs/INSTALL.md troubleshooting for URL/key/model fixes."
    fi
  else
    warn "Skipping live probe because DISCORD_TOKEN or OLLAMA_MODEL is blank."
  fi
}

banner_and_confirm() {
  printf '%sMaxwell installer%s\n' "$BOLD" "$RESET"
  printf 'Maxwell is a Discord self-bot backed by any OpenAI-compatible LLM. This installer fetches the app, installs dependencies, creates a virtualenv, and walks you through configuration.\n\n'
  printf '%sWarning:%s Maxwell uses discord.py-self/self_bot=True. Self-bots may violate Discord Terms of Service and can put your account at risk.\n' "$YELLOW" "$RESET"
  if [ "$NONINTERACTIVE" != "1" ]; then
    answer=$(prompt "Type I UNDERSTAND to continue" "")
    [ "$answer" = "I UNDERSTAND" ] || fail "confirmation not received"
  else
    warn "Non-interactive mode: continuing after printing the self-bot ToS warning."
  fi
}

final_summary() {
  step "Done"
  cat <<EOF
  Install path: $(pwd -P)
  Start the bot: cd $(pwd -P) && ./run.sh
  Start dashboard/API: cd $(pwd -P) && . .venv/bin/activate && python3 api/api_server.py
  PM2 alternative: pm2 start ecosystem.config.js && pm2 logs maxwell-bot maxwell-api
  Edit configuration later: $(pwd -P)/.env
  Re-run the wizard: ./install.sh --local --reconfigure
  Update later: ./install.sh --local, or git pull --ff-only && ./install.sh --local
EOF
}

main() {
  banner_and_confirm
  install_core_system_deps
  verify_python
  clone_or_update
  install_python_deps
  configure_env
  write_run_script
  offer_systemd
  run_doctor
  final_summary
}

main "$@"
