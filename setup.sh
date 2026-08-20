#!/usr/bin/env bash
# Maxwell installer. Creates a virtualenv, installs the core dependencies,
# and writes a .env with the two values Maxwell actually requires: a Discord
# token and a model endpoint. Everything else is optional and auto-detected.
#
#   ./setup.sh              interactive
#   ./setup.sh --no-input   set up the venv + .env skeleton, ask nothing
#
# Safe to re-run: an existing .env is never overwritten.
set -euo pipefail

cd "$(dirname "$0")"
VENV="${MAXWELL_VENV:-.venv}"
INTERACTIVE=1
[[ "${1:-}" == "--no-input" ]] && INTERACTIVE=0
# No TTY (CI, piped install) means we cannot prompt.
[[ -t 0 ]] || INTERACTIVE=0

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

# ── python ────────────────────────────────────────────────────────────────
say "Checking Python"
if ! command -v python3 >/dev/null; then
	echo "python3 not found. Install Python 3.11+ and re-run." >&2
	exit 1
fi
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [[ "$PY_OK" != "1" ]]; then
	echo "Python 3.11+ required (found $(python3 -V))." >&2
	exit 1
fi
ok "$(python3 -V)"

# ── virtualenv + core deps ────────────────────────────────────────────────
say "Installing core dependencies"
if [[ ! -d "$VENV" ]]; then
	python3 -m venv "$VENV" || {
		echo "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
		exit 1
	}
	ok "created $VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
ok "core dependencies installed"

if [[ $INTERACTIVE -eq 1 ]]; then
	read -rp "  Install optional extras too (voice, YouTube, web search, TTS)? [y/N] " extras
	if [[ "${extras,,}" == y* ]]; then
		python -m pip install --quiet -r requirements-optional.txt && ok "extras installed"
	fi
fi

# ── .env ──────────────────────────────────────────────────────────────────
say "Configuring .env"
if [[ -f .env ]]; then
	ok ".env already exists — leaving it alone"
else
	cp .env.example .env
	chmod 600 .env
	if [[ $INTERACTIVE -eq 1 ]]; then
		set_env() {  # set_env KEY VALUE — replace the first KEY= line
			python3 - "$1" "$2" <<-'PY'
			import re, sys, pathlib
			key, value = sys.argv[1], sys.argv[2]
			path = pathlib.Path(".env")
			text = path.read_text()
			new, n = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, count=1, flags=re.M)
			path.write_text(new if n else text + f"\n{key}={value}\n")
			PY
		}
		echo "  Two values are required. Press Enter to skip and edit .env by hand later."
		read -rp "  Discord token: " token
		[[ -n "${token:-}" ]] && set_env DISCORD_TOKEN "$token" && ok "token saved"
		echo "  Model endpoint — e.g. http://localhost:11434 (Ollama),"
		echo "  https://openrouter.ai/api/v1, https://api.openai.com/v1"
		read -rp "  Base URL [http://localhost:11434]: " base
		set_env OLLAMA_BASE_URL "${base:-http://localhost:11434}"
		read -rp "  Model name: " model
		[[ -n "${model:-}" ]] && set_env OLLAMA_MODEL "$model"
		read -rp "  API key (blank for local Ollama): " key
		[[ -n "${key:-}" ]] && set_env OLLAMA_API_KEY "$key"
		read -rp "  Your Discord user ID (for admin commands, optional): " owner
		[[ -n "${owner:-}" ]] && set_env MAXWELL_OWNER_IDS "$owner"
		read -rp "  Dashboard password (optional): " adminpw
		[[ -n "${adminpw:-}" ]] && set_env MAXWELL_ADMIN_PASSWORD "$adminpw"
	else
		warn "copied .env.example to .env — fill in DISCORD_TOKEN and OLLAMA_MODEL"
	fi
fi

# ── report ────────────────────────────────────────────────────────────────
python3 doctor.py || true

say "Next"
echo "  source $VENV/bin/activate"
echo "  python3 bot.py            # the bot"
echo "  python3 api/api_server.py # dashboard/API (optional)"
echo "  pm2 start ecosystem.config.js   # or run both under PM2"
