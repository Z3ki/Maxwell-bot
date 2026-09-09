# Installing Maxwell

## Fastest path

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

The installer explains that Maxwell is a Discord self-bot, warns that self-bots may violate Discord's Terms of Service, clones/updates the repo (`MAXWELL_REPO_URL`, defaulting to the upstream GitHub URL), writes `.env`, and runs Maxwell **in Docker**. Host Python, ffmpeg, and pip packages are not used at runtime.

It will ask for:

1. Discord user token, and optionally an official bot token (`DISCORD_BOT_TOKEN`) used if the user token is rejected or to cover extra servers as the same Maxwell.
2. LLM provider, model, and optional API key.
3. Discord owner user ID(s).
4. Dashboard/admin password.
5. Whether to enable token-spending background loops.

Prompts read from `/dev/tty`, so they work even when the script itself arrives through `curl | bash`. If no TTY exists, set environment variables and run non-interactively.

## Unattended install

```bash
MAXWELL_NONINTERACTIVE=1 \
MAXWELL_INSTALL_DIR="$HOME/maxwell" \
DISCORD_TOKEN="your-discord-user-token" \
DISCORD_BOT_TOKEN="your-discord-bot-token" \
OLLAMA_BASE_URL="https://openrouter.ai/api/v1" \
OLLAMA_MODEL="moonshotai/kimi-k2.6:free" \
OLLAMA_API_KEY="your-openrouter-key" \
MAXWELL_OWNER_IDS="123456789012345678" \
MAXWELL_ADMIN_PASSWORD="change-me" \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh)"
```

To install from a fork or mirror, set `MAXWELL_REPO_URL` (and optionally `MAXWELL_BRANCH`). Identity (`BOT_NAME`, `CREATOR_NAME`, `CREATOR_ID`, `MAXWELL_OWNER_IDS`, `BOT_INVITE_URL`, and related IDs) is configured in `.env` after install — blank IDs mean no baked-in owner. See [CONFIGURATION.md](CONFIGURATION.md).

For sandboxes or CI where Docker must not be installed by the script, add `MAXWELL_SKIP_SYSTEM_DEPS=1`. Use it only after Docker Engine + Compose already work as your user.

## Requirements

| Requirement | Notes |
|---|---|
| OS | Debian/Ubuntu, Fedora/RHEL, Arch, or macOS with Docker Desktop. |
| Docker | Engine + Compose. Linux uses host networking (`docker-compose.yml`); macOS/Windows uses `docker-compose.bridge.yml`. |
| Python on the host | Only needed once, to write `.env` during install. Maxwell itself runs in the image (Python 3.12). |
| Disk/RAM | A few hundred MB for the checkout; more for Docker images and local LLM models. 1 GB+ RAM is recommended for the bot process; local models need much more. |
| Network | GitHub, PyPI (image build), Discord, and your LLM endpoint. |

## Manual install

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at least:

```ini
DISCORD_TOKEN=your-discord-user-token
# Optional official bot token — fallback, or dual-account as one Maxwell
# DISCORD_BOT_TOKEN=your-discord-bot-token
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
MAXWELL_HOST_BIND=/absolute/path/to/this/checkout
# Optional. Default is public/bot in the checkout (already bind-mounted).
# If Caddy serves sites from somewhere else, set the same absolute path here
# so docker compose mounts it — otherwise create_site 404s on the public URL.
# MAXWELL_SITE_DIR=/var/www/maxwell/bot
# MAXWELL_PUBLIC_BASE_URL=https://maxwell.example.com
```

Optional identity (`BOT_NAME`, `CREATOR_NAME`, `CREATOR_ID`, `BOT_INVITE_URL`, …) is documented in [CONFIGURATION.md](CONFIGURATION.md). Empty IDs mean no baked-in owner.

Then:

```bash
docker compose up -d --build
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```

The dashboard/API starts in the same container on port 8765.

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

## Optional features

The Docker image already includes the optional Python packages and system tools (ffmpeg, opus, espeak-ng, Node, Chromium). Features still honour `ENABLE_*=auto|true|false` in `.env`.

| Feature | What it needs besides the image |
|---|---|
| Web search | nothing extra |
| YouTube | nothing extra (Node is in the image) |
| Video input | nothing extra (ffmpeg is in the image) |
| Voice channels | a reachable Discord voice UDP path; host networking on Linux |
| TTS | nothing extra for espeak/gTTS |
| RAG memory | a reachable embeddings endpoint, e.g. `ollama pull qwen3-embedding:0.6b` on the host |
| Shell tool | docker.sock mounted into the Maxwell container (Compose does this) |

## Docker

Maxwell **is** a container. Compose bind-mounts this checkout at `/app` and docker.sock (read-only) so:

- The bot/API run isolated from host Python, with dropped capabilities, `no-new-privileges`, 4 GB / 2 CPU / 2048 pids, and log rotation.
- The `shell` sandbox and `site_server` backends are sibling containers on the host daemon.
- `MAXWELL_HOST_BIND` is the host path of the checkout, used when those siblings bind-mount files.
- Images are multi-stage / slim with BuildKit caches so `docker compose up --build` is incremental.

Linux uses `network_mode: host` so `localhost` Ollama, site backends on `127.0.0.1:8800-8899`, and the dashboard on `:8765` work as before. macOS/Windows use `docker-compose.bridge.yml`; the installer rewrites `localhost` in `.env` to `host.docker.internal`.

## Running Maxwell

```bash
cd ~/maxwell
./run.sh -d
docker compose logs -f maxwell
docker compose exec maxwell python3 doctor.py
docker compose down
```

Dashboard: `http://127.0.0.1:8765`.

For reverse proxying the dashboard and generated sites, adapt [`examples/Caddyfile.example`](../examples/Caddyfile.example). It proxies `/api/*`, `/data/*`, and generated site backend routes to `127.0.0.1:8765`.

## Upgrading from a host / venv / PM2 install

If Maxwell is still running on the host (`python3 bot.py`, `.venv`, `pm2 start ecosystem.config.js`, or a systemd user unit), do not keep that process after this release — two clients on the same Discord token will fight.

```bash
cd ~/maxwell
git pull --ff-only
./install.sh --local
```

That keeps `.env`, `data/`, and generated sites, sets `MAXWELL_HOST_BIND`, stops `maxwell-bot` / `maxwell-api` in PM2 (and a systemd user service named `maxwell` if you had one), and starts the Docker stack. Host Ollama is left running. After Discord looks healthy you can delete `.venv`. `pm2 restart` on the old names will not bring the host bot back; use `docker compose up -d` from here on.

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
cd ~/maxwell
docker compose down --rmi local
rm -rf ~/maxwell
```

Ollama, if you installed it on the host, is separate.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker info` fails | Start Docker. On Linux, add your user to the `docker` group and re-login. |
| Compose cannot find the image | `docker compose up -d --build` from the checkout. |
| Shell/site_server bind-mounts fail | Set `MAXWELL_HOST_BIND` to the absolute host path of the checkout (install.sh does this). |
| `doctor.py --probe` returns 404 | Check `OLLAMA_BASE_URL` and model name. Bare Ollama hosts should be `http://localhost:11434`; hosted APIs usually include `/v1`. |
| `doctor.py --probe` returns 401/403 | Check `OLLAMA_API_KEY` or provider account access. |
| Local Ollama unreachable from the container | Linux host networking should see `localhost`. On Docker Desktop, use `http://host.docker.internal:11434`. |
| Discord token invalid | Re-copy the `authorization` header from a logged-in Discord browser session. |
| `curl | bash` prompts do not appear | Run from an interactive terminal with `/dev/tty`, or use the unattended environment variables. |
| Dashboard returns 503 | Set `MAXWELL_ADMIN_PASSWORD` in `.env` and `docker compose up -d`. |
