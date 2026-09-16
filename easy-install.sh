#!/usr/bin/env bash
set -euo pipefail

if [ -t 1 ]; then
  BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RESET='\033[0m'
else
  BOLD=''; GREEN=''; YELLOW=''; RESET=''
fi

say() { printf '%b\n' "$*"; }
ok() { printf '  %b✓%b %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  ERROR: %s\n' "$*" >&2; exit 1; }

INSTALL_DIR="${MAXWELL_INSTALL_DIR:-$HOME/maxwell}"
REPO_URL="${MAXWELL_REPO_URL:-https://github.com/Z3ki/Maxwell-bot.git}"
BRANCH="${MAXWELL_BRANCH:-main}"
TTY=""
[ -r /dev/tty ] && [ -w /dev/tty ] && TTY=/dev/tty || true

prompt() {
  local label="$1" default="${2:-}" answer=""
  if [ -z "$TTY" ]; then printf '%s' "$default"; return; fi
  if [ -n "$default" ]; then
    printf '  %s [%s]: ' "$label" "$default" > "$TTY"
  else
    printf '  %s: ' "$label" > "$TTY"
  fi
  IFS= read -r answer < "$TTY" || true
  printf '%s' "${answer:-$default}"
}

prompt_secret() {
  local label="$1" default="${2:-}" answer=""
  if [ -z "$TTY" ]; then printf '%s' "$default"; return; fi
  if [ -n "$default" ]; then
    printf '  %s [Enter keeps current value]: ' "$label" > "$TTY"
  else
    printf '  %s: ' "$label" > "$TTY"
  fi
  IFS= read -r -s answer < "$TTY" || true
  printf '\n' > "$TTY"
  printf '%s' "${answer:-$default}"
}

yes_no() {
  local label="$1" default="${2:-yes}" answer
  if [ -z "$TTY" ]; then [ "$default" = yes ] && return 0 || return 1; fi
  while :; do
    answer=$(prompt "$label (yes/no)" "$default")
    case "${answer,,}" in yes|y) return 0 ;; no|n) return 1 ;; *) warn "Please answer yes or no." ;; esac
  done
}

require_host_tools() {
  command -v git >/dev/null 2>&1 || fail "git is required. Install Git, then run this again."
  command -v curl >/dev/null 2>&1 || fail "curl is required. Install curl, then run this again."
  command -v python3 >/dev/null 2>&1 || fail "Python 3 is required for setup. Maxwell itself runs in Docker."
}

get_repo() {
  say "${BOLD}Getting Maxwell${RESET}"
  if [ ! -e "$INSTALL_DIR" ]; then
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "cloned to $INSTALL_DIR"
  elif [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only || warn "Could not fast-forward the existing checkout; keeping local files."
    ok "using existing checkout at $INSTALL_DIR"
  else
    fail "$INSTALL_DIR exists but is not a Git checkout. Move it or set MAXWELL_INSTALL_DIR."
  fi
  cd "$INSTALL_DIR"
}

set_value() {
  SET_ENV_VALUE="$2" python3 scripts/set_env.py .env "$1"
}

choose_provider() {
  local choice
  AI_API_URL="${AI_API_URL:-}"
  AI_MODEL="${AI_MODEL:-}"
  AI_API_KEY="${AI_API_KEY:-}"
  PROVIDER_KIND="custom"

  if [ -n "$AI_API_URL" ] && [ -n "$AI_MODEL" ]; then
    return
  fi

  say ""
  say "${BOLD}2/3 - AI provider${RESET}"
  if [ -z "$TTY" ]; then
    AI_API_URL="${AI_API_URL:-http://localhost:11434}"
    AI_MODEL="${AI_MODEL:-qwen3:8b}"
    PROVIDER_KIND="ollama"
    return
  fi

  cat > "$TTY" <<'MENU'
  Pick what powers Maxwell:
    1) Ollama on this computer
    2) OpenRouter
    3) OpenAI
    4) LM Studio
    5) Custom OpenAI-compatible API
MENU
  choice=$(prompt "Provider" "1")
  case "$choice" in
    1)
      PROVIDER_KIND="ollama"
      AI_API_URL="http://localhost:11434"
      AI_MODEL="qwen3:8b"
      AI_API_KEY=""
      ;;
    2)
      PROVIDER_KIND="openrouter"
      AI_API_URL="https://openrouter.ai/api/v1"
      AI_MODEL="moonshotai/kimi-k2.6:free"
      ;;
    3)
      PROVIDER_KIND="openai"
      AI_API_URL="https://api.openai.com/v1"
      AI_MODEL="gpt-4.1-mini"
      ;;
    4)
      PROVIDER_KIND="lmstudio"
      AI_API_URL="http://localhost:1234/v1"
      AI_MODEL="local-model"
      AI_API_KEY=""
      ;;
    5)
      PROVIDER_KIND="custom"
      AI_API_URL=$(prompt "AI API URL" "http://localhost:8000/v1")
      AI_MODEL=""
      ;;
    *)
      warn "Unknown choice; using Ollama defaults."
      PROVIDER_KIND="ollama"
      AI_API_URL="http://localhost:11434"
      AI_MODEL="qwen3:8b"
      AI_API_KEY=""
      ;;
  esac

  AI_MODEL=$(prompt "Model" "$AI_MODEL")
  case "$PROVIDER_KIND" in
    openrouter|openai|custom)
      AI_API_KEY=$(prompt_secret "API key (blank only if your API does not need one)" "$AI_API_KEY")
      ;;
  esac

  [ -n "$AI_API_URL" ] || fail "AI_API_URL cannot be empty."
  [ -n "$AI_MODEL" ] || fail "AI_MODEL cannot be empty."
}

maybe_install_ollama() {
  [ "$PROVIDER_KIND" = "ollama" ] || return 0
  command -v ollama >/dev/null 2>&1 && return 0
  [ -n "$TTY" ] || { warn "Ollama is not installed. Install it before starting Maxwell, or use AI_API_URL for another provider."; return 0; }
  if ! yes_no "Ollama is missing. Install it now?" "yes"; then
    warn "Skipping Ollama install. Maxwell will need a reachable API at $AI_API_URL."
    return 0
  fi
  if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
    warn "Automatic Ollama install is only handled here on Linux. Install it from ollama.com, then rerun Maxwell."
    return 0
  fi
  say "  Installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
  if command -v ollama >/dev/null 2>&1; then
    say "  Pulling $AI_MODEL..."
    ollama pull "$AI_MODEL" || warn "Model pull failed; you can run: ollama pull $AI_MODEL"
  fi
}

write_fresh_env() {
  cp .env.simple.example .env
  chmod 600 .env
  set_value DISCORD_BOT_TOKEN "$DISCORD_BOT_TOKEN"
  set_value AI_API_URL "$AI_API_URL"
  set_value AI_MODEL "$AI_MODEL"
  set_value AI_API_KEY "$AI_API_KEY"
  set_value MAXWELL_OWNER_IDS "$MAXWELL_OWNER_IDS"
  if [ -n "$MAXWELL_OWNER_IDS" ]; then
    set_value CREATOR_ID "${MAXWELL_OWNER_IDS%%,*}"
  fi
  set_value MAXWELL_ADMIN_USER "admin"
  set_value MAXWELL_ADMIN_PASSWORD "$MAXWELL_ADMIN_PASSWORD"
  ok "wrote a small .env using AI_API_URL / AI_MODEL / AI_API_KEY"
}

configure() {
  say ""
  if [ -f .env ]; then
    say "${BOLD}Existing config found${RESET}"
    python3 scripts/migrate_ai_env.py .env
    ok "kept your settings and migrated the primary provider names to AI_*"
    return
  fi

  say "${BOLD}1/3 - Discord${RESET}"
  say "  Create a bot in the Discord Developer Portal and enable Message Content, Server Members, and Presence intents."
  DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"
  DISCORD_BOT_TOKEN=$(prompt_secret "Discord bot token" "$DISCORD_BOT_TOKEN")
  [ -n "$DISCORD_BOT_TOKEN" ] || fail "A Discord bot token is required."

  choose_provider
  maybe_install_ollama

  say ""
  say "${BOLD}3/3 - Owner${RESET}"
  say "  Optional: enable Discord Developer Mode, right-click yourself, then Copy User ID."
  MAXWELL_OWNER_IDS="${MAXWELL_OWNER_IDS:-}"
  MAXWELL_OWNER_IDS=$(prompt "Owner Discord ID (optional)" "$MAXWELL_OWNER_IDS")

  if command -v openssl >/dev/null 2>&1; then
    MAXWELL_ADMIN_PASSWORD=$(openssl rand -hex 16)
  else
    MAXWELL_ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
  fi

  write_fresh_env
  say ""
  say "  Dashboard username: admin"
  say "  Dashboard password: ${MAXWELL_ADMIN_PASSWORD}"
  say "  Save that password. You can change it later in .env."
}

main() {
  say "${BOLD}Maxwell easy installer${RESET}"
  say "Three short steps, then the normal Docker installer takes over."
  require_host_tools
  get_repo
  configure
  MAXWELL_INSTALL_DIR="$INSTALL_DIR" bash install.sh
  say ""
  ok "Maxwell setup finished"
  say "  Config: $INSTALL_DIR/.env"
  say "  Friendly AI settings: AI_API_URL, AI_MODEL, AI_API_KEY"
  say "  Advanced settings: $INSTALL_DIR/.env.example"
}

main "$@"
