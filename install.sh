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
CONFIGURE_ONLY=0
NONINTERACTIVE="${MAXWELL_NONINTERACTIVE:-0}"
SKIP_SYSTEM_DEPS="${MAXWELL_SKIP_SYSTEM_DEPS:-0}"
TTY=""
OS_FAMILY=""
COMPOSE=()

usage() {
  cat <<'EOF'
Maxwell installer (Docker)

Usage:
  bash install.sh [options]

Maxwell runs in Docker so host Python, ffmpeg, and package versions cannot
fight it. Setup needs Git, curl, and Python 3; running needs Docker + Compose.

Options:
  --help              Show this help.
  --reconfigure       Run the configuration wizard even when .env exists.
  --non-interactive   Read all answers from environment variables.
  --dir <path>        Install/update Maxwell in this directory.
  --local             Configure the current checkout instead of cloning.
  --configure-only    Prepare .env and run.sh without installing or starting Docker.

Useful environment variables:
  MAXWELL_INSTALL_DIR, MAXWELL_REPO_URL, MAXWELL_BRANCH,
  MAXWELL_NONINTERACTIVE=1, MAXWELL_SKIP_SYSTEM_DEPS=1,
  DISCORD_BOT_TOKEN, OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY,
  MAXWELL_OWNER_IDS, MAXWELL_ADMIN_PASSWORD,
  BOT_NAME, CREATOR_NAME, CREATOR_ID, COMMAND_PREFIX
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --configure-only) CONFIGURE_ONLY=1 ;;
    --no-extras) warn "--no-extras is ignored; extras ship in the Docker image." ;;
    --non-interactive) NONINTERACTIVE=1 ;;
    --dir) shift; [ "$#" -gt 0 ] || fail "--dir requires a path"; INSTALL_DIR="$1" ;;
    --local) LOCAL_MODE=1; INSTALL_DIR="$SCRIPT_DIR"; SKIP_SYSTEM_DEPS="${MAXWELL_SKIP_SYSTEM_DEPS:-1}" ;;
    *) fail "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ -r /dev/tty ] && [ -w /dev/tty ] && ( : <> /dev/tty ) 2>/dev/null; then
  TTY=/dev/tty
elif [ "$NONINTERACTIVE" != "1" ]; then
  NONINTERACTIVE=1
  warn "No controlling TTY is available; switching to non-interactive mode."
  warn "Set DISCORD_BOT_TOKEN, OLLAMA_MODEL, and other MAXWELL_* variables, then re-run with --reconfigure if needed."
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
  if [ "$NONINTERACTIVE" = "1" ] || [ -z "${TTY:-}" ]; then
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
    case "$env_value" in yes|YES|Yes|y|Y|1|true|TRUE|on|ON) default_value=yes ;; no|NO|No|n|N|0|false|FALSE|off|OFF) default_value=no ;; esac
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
      fail "sudo is required to install Docker as a non-root user, or set MAXWELL_SKIP_SYSTEM_DEPS=1 after installing Docker yourself."
    fi
    warn "Using sudo. You may be prompted for your password."
    sudo "$@"
  fi
}

detect_os() {
  if command -v apt-get >/dev/null 2>&1; then OS_FAMILY=apt; return; fi
  if command -v dnf >/dev/null 2>&1; then OS_FAMILY=dnf; return; fi
  if command -v pacman >/dev/null 2>&1; then OS_FAMILY=pacman; return; fi
  if [ "$(uname -s 2>/dev/null || printf unknown)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then OS_FAMILY=brew; return; fi
    fail "macOS detected but Homebrew is missing. Install Docker Desktop, then re-run."
  fi
  cat >&2 <<EOF
Unsupported OS/package manager.
Install Docker Engine + Compose, then re-run with MAXWELL_SKIP_SYSTEM_DEPS=1.
EOF
  exit 1
}

docker_info_ok() {
  docker info >/dev/null 2>&1
}

resolve_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
    return 0
  fi
  return 1
}

compose_file_for_host() {
  if [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ]; then
    printf '%s' "docker-compose.yml"
  else
    printf '%s' "docker-compose.bridge.yml"
  fi
}

rewrite_localhost_for_bridge() {
  [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ] && return 0
  [ -f .env ] || return 0
  python3 scripts/rewrite_bridge_env.py .env
  warn "Non-Linux Docker: local service addresses now use host.docker.internal; the API listens inside the container on 0.0.0.0 (published on host loopback only)."
}

install_docker() {
  if docker_info_ok && resolve_compose; then
    ok "Docker and Compose are ready"
    return 0
  fi
  [ "$SKIP_SYSTEM_DEPS" = "1" ] && fail "Docker is not usable and MAXWELL_SKIP_SYSTEM_DEPS=1. Install Docker Engine + Compose, then re-run."
  detect_os
  step "Installing Docker"
  case "$OS_FAMILY" in
    apt|dnf)
      curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
      run_as_root sh /tmp/get-docker.sh
      rm -f /tmp/get-docker.sh
      ;;
    pacman)
      run_as_root pacman -Sy --needed --noconfirm docker docker-compose
      run_as_root systemctl enable --now docker || true
      ;;
    brew)
      warn "Installing Docker Desktop with Homebrew. Start Docker Desktop after installation, then re-run."
      brew install --cask docker
      ;;
  esac
  target_user="${USER:-$(id -un 2>/dev/null || printf '')}"
  if command -v docker >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ] && [ -n "$target_user" ] && getent group docker >/dev/null 2>&1; then
    run_as_root usermod -aG docker "$target_user" || true
    warn "Added $target_user to the docker group. If docker info still fails, log out and back in."
  fi
  if command -v systemctl >/dev/null 2>&1; then
    run_as_root systemctl enable --now docker || true
  fi
  if docker_info_ok && resolve_compose; then
    ok "Docker and Compose are ready"
    return 0
  fi
  if run_as_root docker info >/dev/null 2>&1; then
    fail "Docker works with sudo but not as this user. Log out and back in (docker group), then re-run."
  fi
  fail "Docker is required. Install Docker Engine + Compose and re-run."
}

clone_or_update() {
  step "Getting Maxwell"
  if [ "$LOCAL_MODE" != "1" ]; then
    command -v git >/dev/null 2>&1 || fail "git is required to fetch Maxwell."
  fi
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
  command -v python3 >/dev/null 2>&1 || fail "python3 is needed once to write .env. Maxwell itself runs in Docker."
  SET_ENV_VALUE="$2" python3 scripts/set_env.py .env "$1"
}

copy_env_if_needed() {
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    ok "created .env from .env.example"
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
  # Explicit ENABLE_REM is also an override when updating a legacy REM_ENABLED.
  if printenv ENABLE_REM >/dev/null 2>&1 && ! printenv REM_ENABLED >/dev/null 2>&1; then
    export REM_ENABLED="$ENABLE_REM"
  fi
  # Reconfiguration starts from the saved settings, not fresh-install defaults.
  # Never source .env: passwords and other values are data, not shell commands.
  # Explicit exported variables still take precedence for unattended updates.
  if [ -f .env ]; then
    defaults_file=$(mktemp)
    if ! python3 scripts/env_defaults.py .env \
      DISCORD_BOT_TOKEN DISCORD_TOKEN OLLAMA_BASE_URL OLLAMA_MODEL OLLAMA_API_KEY \
      BOT_NAME CREATOR_NAME CREATOR_ID MAXWELL_OWNER_IDS COMMAND_PREFIX \
      MAXWELL_ADMIN_USER MAXWELL_ADMIN_PASSWORD ENABLE_AUTONOMY ENABLE_REM REM_ENABLED ENABLE_SHELL \
      > "$defaults_file"; then
      rm -f "$defaults_file"
      fail "Could not read existing .env; no settings were changed."
    fi
    while IFS= read -r -d '' name && IFS= read -r -d '' value; do
      if ! printenv "$name" >/dev/null 2>&1; then
        export "$name=$value"
      fi
    done < "$defaults_file"
    rm -f "$defaults_file"
  fi
  copy_env_if_needed

  printf '\n%sStep 1/5: Discord bot token%s\n' "$BOLD" "$RESET"
  printf '  Create an application at https://discord.com/developers/applications, add a Bot, copy the bot token.\n'
  printf '  Enable Privileged Gateway Intents: Message Content, Server Members, Presence.\n'
  printf '  Invite the bot with applications.commands omitted is fine; Maxwell uses prefix commands, not slash commands.\n'
  bot_token=$(prompt_secret "Discord bot token" "${DISCORD_BOT_TOKEN:-${DISCORD_TOKEN:-}}")
  if [ -n "$bot_token" ]; then set_env_value DISCORD_BOT_TOKEN "$bot_token"; ok "Discord bot token saved"; else warn "DISCORD_BOT_TOKEN left blank; set it in .env before starting."; fi

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
      ollama_choice=$(yes_no "Install Ollama on this host and pull the selected model?" "no" "")
      if [ "$ollama_choice" = "yes" ]; then
        if [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ]; then
          curl -fsSL https://ollama.com/install.sh -o /tmp/ollama-install.sh
          sh /tmp/ollama-install.sh
          rm -f /tmp/ollama-install.sh
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

  printf '\n%sStep 3/5: Identity (name, owner, prefix)%s\n' "$BOLD" "$RESET"
  printf '  A fresh clone is not owned by anyone until you set these. Display names are labels, not a baked-in identity.\n'
  bot_name=$(prompt "Bot display name" "${BOT_NAME:-Maxwell}")
  [ -n "$bot_name" ] || bot_name="Maxwell"
  set_env_value BOT_NAME "$bot_name"

  creator_name=$(prompt "Owner/creator name (optional)" "${CREATOR_NAME:-}")
  if [ -n "$creator_name" ]; then
    set_env_value CREATOR_NAME "$creator_name"
  fi

  printf '  Enable Discord Developer Mode, right-click yourself, and choose Copy User ID. Use commas for multiple owners.\n'
  owner=$(prompt "Owner Discord ID(s), optional" "${MAXWELL_OWNER_IDS:-}")
  if [ -n "$owner" ]; then
    set_env_value MAXWELL_OWNER_IDS "$owner"
  else
    warn "MAXWELL_OWNER_IDS left blank; admin commands will be denied."
  fi

  creator_id="${CREATOR_ID:-}"
  if [ -z "$creator_id" ] && [ -n "$owner" ]; then
    creator_id="${owner%%,*}"
    creator_id="${creator_id#"${creator_id%%[![:space:]]*}"}"
    creator_id="${creator_id%"${creator_id##*[![:space:]]}"}"
  fi
  if [ -n "$creator_id" ]; then
    set_env_value CREATOR_ID "$creator_id"
    if [ -z "${CREATOR_ID:-}" ]; then
      ok "CREATOR_ID set from the first owner ID"
    fi
  fi

  cmd_prefix=$(prompt "Command prefix (optional)" "${COMMAND_PREFIX:-,}")
  [ -n "$cmd_prefix" ] || cmd_prefix=","
  set_env_value COMMAND_PREFIX "$cmd_prefix"

  printf '\n%sStep 4/5: Dashboard credentials%s\n' "$BOLD" "$RESET"
  printf '  Empty MAXWELL_ADMIN_PASSWORD makes the dashboard/admin API answer 503. Press Enter interactively to generate one.\n'
  admin_user_default="${MAXWELL_ADMIN_USER:-admin}"
  admin_user=$(prompt "Dashboard admin username" "$admin_user_default")
  [ -n "$admin_user" ] || admin_user="admin"
  set_env_value MAXWELL_ADMIN_USER "$admin_user"

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
  rem=$(yes_no "Enable REM memory consolidation?" "no" "${REM_ENABLED:-${ENABLE_REM:-}}")
  if [ "$autonomy" = "yes" ]; then
    set_env_value ENABLE_AUTONOMY true
  else
    set_env_value ENABLE_AUTONOMY false
  fi
  if [ "$rem" = "yes" ]; then
    set_env_value ENABLE_REM true
    set_env_value REM_ENABLED true
  else
    set_env_value ENABLE_REM false
    set_env_value REM_ENABLED false
  fi

  set_env_value ENABLE_SHELL "${ENABLE_SHELL:-auto}"
}

write_host_bind() {
  host_bind="$(pwd -P)"
  set_env_value MAXWELL_HOST_BIND "$host_bind"
  ok "MAXWELL_HOST_BIND=$host_bind (so sibling containers can bind-mount this checkout)"
}

write_run_script() {
  cat > run.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -z "${COMPOSE_FILE:-}" ]; then
  if [ "$(uname -s 2>/dev/null || printf unknown)" = "Linux" ]; then
    export COMPOSE_FILE=docker-compose.yml
  else
    export COMPOSE_FILE=docker-compose.bridge.yml
  fi
fi
if docker compose version >/dev/null 2>&1; then
  exec docker compose up "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose up "$@"
fi
echo "docker compose is required" >&2
exit 1
EOF
  chmod +x run.sh
  ok "wrote run.sh (docker compose up)"
}

stop_host_maxwell() {
  # Old installs ran bot.py from a venv under PM2 or systemd. Two Maxwells
  # on one Discord token fight each other, so the host process has to go
  # before the container starts. .env and data/ stay.
  step "Checking for a host Maxwell (venv / PM2 / systemd)"
  stopped=0
  if command -v pm2 >/dev/null 2>&1; then
    for name in maxwell-bot maxwell-api; do
      if pm2 describe "$name" >/dev/null 2>&1; then
        pm2 stop "$name" >/dev/null 2>&1 || true
        pm2 delete "$name" >/dev/null 2>&1 || true
        ok "stopped pm2 process $name"
        stopped=1
      fi
    done
    if [ "$stopped" = "1" ]; then
      pm2 save >/dev/null 2>&1 || true
    fi
  fi
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user is-active --quiet maxwell 2>/dev/null; then
      systemctl --user disable --now maxwell >/dev/null 2>&1 || true
      ok "stopped systemd user service maxwell"
      stopped=1
    fi
  fi
  if [ "$stopped" = "1" ]; then
    warn "Host Maxwell was stopped so Docker can take over. .env and data/ are unchanged. Delete .venv after this looks healthy."
  elif [ -d .venv ]; then
    warn "Found an old .venv from a host install. Maxwell now runs in Docker; you can delete .venv later."
  else
    ok "no host Maxwell process to stop"
  fi
}

start_stack() {
  step "Building and starting Maxwell in Docker"
  mkdir -p data public/bot shelldocker logs data/exports
  export COMPOSE_FILE
  COMPOSE_FILE="$(compose_file_for_host)"
  rewrite_localhost_for_bridge
  if ! resolve_compose; then
    fail "docker compose is not available"
  fi
  export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
  # Keep the existing host service alive if the image cannot be built.
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" build
  stop_host_maxwell
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d --no-build
  ok "container started"
}

run_doctor() {
  step "Verifying installation"
  COMPOSE_FILE="$(compose_file_for_host)"
  resolve_compose || return 0
  if "${COMPOSE[@]}" -f "$COMPOSE_FILE" exec -T maxwell python3 doctor.py; then
    ok "doctor.py reports the install is ready"
  else
    warn "doctor.py found issues. Inspect with: docker compose -f $COMPOSE_FILE logs maxwell"
  fi
}

banner_and_confirm() {
  printf '%sMaxwell installer%s\n' "$BOLD" "$RESET"
  printf 'Maxwell is an official Discord bot backed by any OpenAI-compatible LLM. This installer fetches the app, writes .env, and runs Maxwell in Docker.\n\n'
  printf 'You need a bot token from https://discord.com/developers/applications (not a user token).\n'
}

final_summary() {
  step "Done"
  compose="$(compose_file_for_host)"
  cat <<EOF
  Install path: $(pwd -P)
  Maxwell runs in Docker, not on the host Python.

  Start:     cd $(pwd -P) && ./run.sh -d
             (or: docker compose -f $compose up -d)
  Logs:      docker compose -f $compose logs -f maxwell
  Stop:      docker compose -f $compose down
  Doctor:    docker compose -f $compose exec maxwell python3 doctor.py
  Dashboard: http://127.0.0.1:8765
  Edit config: $(pwd -P)/.env   then   docker compose -f $compose up -d
  Reconfigure: ./install.sh --local --reconfigure
  Update:      git pull --ff-only && ./install.sh --local
  Old venv/PM2 install: that same update command stops host Maxwell and starts Docker.
EOF
}

main() {
  banner_and_confirm
  command -v python3 >/dev/null 2>&1 || fail "python3 is required to prepare .env. Install Python 3, then re-run."
  clone_or_update
  configure_env
  write_host_bind
  write_run_script
  if [ "$CONFIGURE_ONLY" = "1" ]; then
    rewrite_localhost_for_bridge
    ok "Configuration prepared. Edit .env as needed, then run ./run.sh -d --build."
    return 0
  fi
  install_docker
  start_stack
  run_doctor
  final_summary
}

main "$@"
