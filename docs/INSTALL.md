# Installing Maxwell

## Fastest path

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

The installer explains that Maxwell is a Discord self-bot, warns that self-bots may violate Discord's Terms of Service, installs system packages, clones/updates `https://github.com/Z3ki/Maxwell-bot.git`, creates `.venv`, installs Python dependencies, asks for configuration, runs `doctor.py`, writes `run.sh`, and prints start/update instructions.

It will ask for:

1. Discord user token.
2. LLM provider, model, and optional API key.
3. Discord owner user ID(s).
4. Dashboard/admin password.
5. Whether to enable token-spending background loops.
6. Whether to install Docker or disable the shell tool.

Prompts read from `/dev/tty`, so they work even when the script itself arrives through `curl | bash`. If no TTY exists, set environment variables and run non-interactively.

## Unattended install

```bash
MAXWELL_NONINTERACTIVE=1 \
MAXWELL_INSTALL_DIR="$HOME/maxwell" \
DISCORD_TOKEN="your-discord-user-token" \
OLLAMA_BASE_URL="https://openrouter.ai/api/v1" \
OLLAMA_MODEL="moonshotai/kimi-k2.6:free" \
OLLAMA_API_KEY="your-openrouter-key" \
MAXWELL_OWNER_IDS="123456789012345678" \
MAXWELL_ADMIN_PASSWORD="change-me" \
MAXWELL_INSTALL_EXTRAS=no \
MAXWELL_INSTALL_DOCKER=no \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh)"
```

For sandboxes or CI where system packages must not be installed, add `MAXWELL_SKIP_SYSTEM_DEPS=1`. Use it only after preinstalling `git`, `curl`, and Python 3.11+ with venv/pip.

## Requirements

| Requirement | Notes |
|---|---|
| OS | Debian/Ubuntu (`apt-get`), Fedora/RHEL (`dnf`), Arch (`pacman`), or macOS with Homebrew. |
| Python | 3.11 or newer, with `venv` and `pip`. |
| Disk/RAM | A few hundred MB for the checkout and venv; more for optional packages, Docker images, and local LLM models. 1 GB+ RAM is recommended for the bot process; local models need much more. |
| Network | GitHub, PyPI, Discord, and your LLM endpoint. |
| Optional Docker | Required only for the `shell` tool. |
| Optional media packages | `ffmpeg`, opus/libopus, libsodium, `espeak-ng`, and Node.js unlock video, voice, TTS, and YouTube helpers. |

## Manual install

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at least:

```ini
DISCORD_TOKEN=your-discord-user-token
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
```

Then verify and run:

```bash
python3 doctor.py
python3 doctor.py --probe
python3 bot.py
```

The API/dashboard is separate:

```bash
python3 api/api_server.py
```

## Credentials and provider setup

### Discord user token

Maxwell needs a Discord **user** token because it is a self-bot. This may violate Discord ToS.

Common ways to find it in a browser session:

1. Open Discord in a browser.
2. Open Developer Tools.
3. Network tab: click any request to `discord.com/api`, then copy the `authorization` request header.
4. Or Application/Storage: inspect Discord local storage for the token.

Never paste this token into chat, logs, or git.

### Discord owner ID

In Discord, enable **Settings → Advanced → Developer Mode**, right-click yourself, and choose **Copy User ID**. Put one or more IDs in `MAXWELL_OWNER_IDS`, separated by commas.

### OpenRouter

Create a key at `https://openrouter.ai/keys` and use:

```ini
OLLAMA_BASE_URL=https://openrouter.ai/api/v1
OLLAMA_MODEL=moonshotai/kimi-k2.6:free
OLLAMA_API_KEY=your-openrouter-key
```

### OpenAI

Use an OpenAI API key and a model your account can access:

```ini
OLLAMA_BASE_URL=https://api.openai.com/v1
OLLAMA_MODEL=gpt-4.1-mini
OLLAMA_API_KEY=your-openai-key
```

### Ollama

On Linux, install Ollama from `https://ollama.com/install.sh`, then pull a chat model and the default RAG embedding model:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:8b
ollama pull qwen3-embedding:0.6b
```

```ini
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_API_KEY=
MAXWELL_EMBED_BASE_URL=http://localhost:11434
MAXWELL_EMBED_MODEL=qwen3-embedding:0.6b
```

### LM Studio

Start LM Studio's local OpenAI-compatible server, load a model, then use:

```ini
OLLAMA_BASE_URL=http://localhost:1234/v1
OLLAMA_MODEL=the-loaded-model-name
OLLAMA_API_KEY=
```

### Custom OpenAI-compatible endpoint

```ini
OLLAMA_BASE_URL=https://your-provider.example/v1
OLLAMA_MODEL=provider-model-name
OLLAMA_API_KEY=provider-key-if-needed
```

A bare host such as `http://localhost:11434` is normalized by Maxwell with `/v1` appended. A URL that already has a path, such as `https://openrouter.ai/api/v1`, is used as-is.

## Optional features and packages

Install all optional Python packages with:

```bash
python -m pip install -r requirements-optional.txt
```

| Feature | Python package(s) | System package(s) |
|---|---|---|
| Web search | `ddgs` | none |
| YouTube | `yt-dlp`, `yt-dlp-ejs` | `nodejs` or another JS runtime for YouTube challenges |
| Video input | core code | `ffmpeg` |
| Voice channels | `PyNaCl`, `davey`, `discord-ext-voice-recv`, `nvidia-riva-client` | opus/libopus, libsodium, `ffmpeg` |
| TTS | `gTTS` for Google TTS | `espeak-ng` for local TTS, `ffmpeg` for VC playback |
| RAG memory | core code | reachable embeddings endpoint, e.g. `ollama pull qwen3-embedding:0.6b` |
| Shell tool | core code | Docker Engine and reachable daemon |

System package examples:

```bash
# Debian/Ubuntu
sudo apt install ffmpeg libopus0 libsodium-dev espeak-ng nodejs

# Fedora/RHEL
sudo dnf install ffmpeg opus libsodium-devel espeak-ng nodejs

# Arch
sudo pacman -Sy --needed ffmpeg opus libsodium espeak-ng nodejs

# macOS/Homebrew
brew install ffmpeg opus libsodium espeak-ng node
```

## Docker for the shell tool

The shell tool runs inside a Docker container and requires a Docker daemon reachable by the bot user.

- If Docker works, keep `ENABLE_SHELL=true`.
- If Docker is absent or the current user cannot access the daemon, set `ENABLE_SHELL=false`.
- On Linux, after adding a user to the `docker` group, log out and back in before retrying.

The installer never leaves the default shell tool enabled silently when Docker is unavailable; it writes `ENABLE_SHELL=false` unless Docker is selected and reachable.

## Running Maxwell

Foreground bot:

```bash
cd ~/maxwell
./run.sh
```

Equivalent manual command:

```bash
cd ~/maxwell
. .venv/bin/activate
python3 bot.py
```

Dashboard/API:

```bash
cd ~/maxwell
. .venv/bin/activate
python3 api/api_server.py
```

PM2:

```bash
pm2 start ecosystem.config.js
pm2 logs maxwell-bot maxwell-api
```

`ecosystem.config.js` uses `.venv/bin/python3` when it exists and only starts an `ollama` PM2 process when `ollama` is on `PATH` unless `MAXWELL_PM2_OLLAMA=true|false` overrides it.

Linux systemd user service (created optionally by the installer):

```bash
systemctl --user enable --now maxwell
systemctl --user status maxwell
```

For reverse proxying the dashboard and generated sites, start `api/api_server.py` and adapt [`examples/Caddyfile.example`](../examples/Caddyfile.example). It proxies `/api/*`, `/data/*`, and generated site backend routes to `127.0.0.1:8765`.

## Updating and uninstalling

Update:

```bash
cd ~/maxwell
git pull --ff-only
./install.sh --local
```

Re-run the wizard:

```bash
cd ~/maxwell
./install.sh --local --reconfigure
```

Uninstall a one-user install:

```bash
systemctl --user disable --now maxwell 2>/dev/null || true
rm -f ~/.config/systemd/user/maxwell.service
rm -rf ~/maxwell
```

Remove Docker, Node, or system media packages separately with your OS package manager if you installed them only for Maxwell.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Python too old | Install Python 3.11+ and make sure `python3 -V` shows it. |
| `python3 -m venv` fails | Debian/Ubuntu: `sudo apt install python3-venv`. |
| `doctor.py` says core packages missing | Activate `.venv` and run `python -m pip install -r requirements.txt`. |
| `doctor.py --probe` returns 404 | Check `OLLAMA_BASE_URL` and model name. Bare Ollama hosts should be `http://localhost:11434`; hosted APIs usually include `/v1`. |
| `doctor.py --probe` returns 401/403 | Check `OLLAMA_API_KEY` or provider account access. |
| Docker daemon unreachable | Start Docker and verify `docker info`. If permission is denied, add your user to the `docker` group and re-login, or set `ENABLE_SHELL=false`. |
| Discord token invalid | Re-copy the `authorization` header from a logged-in Discord browser session. |
| `pip` build failures for media packages | Install compiler/system headers, upgrade pip, or skip optional extras. Core install does not need optional media packages. |
| macOS bash concerns | The installer avoids Bash 4-only syntax and works with macOS Bash 3.2, but Homebrew is required for packages. |
| `curl | bash` prompts do not appear | Run from an interactive terminal with `/dev/tty`, or use the unattended environment variables. |
| Dashboard returns 503 | Set `MAXWELL_ADMIN_PASSWORD` in `.env` and restart `api/api_server.py`. |
