# Installing Maxwell

## Fastest path

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

The installer clones/updates the repo (`MAXWELL_REPO_URL`, defaulting to the upstream GitHub URL), writes `.env`, and runs Maxwell **in Docker**. Host Python, ffmpeg, and pip packages are not used at runtime.

It will ask for:

1. Official Discord bot token (`DISCORD_BOT_TOKEN`) from the Developer Portal. Enable Message Content, Server Members, and Presence intents.
2. LLM provider, model, and optional API key.
3. Discord owner user ID(s).
4. Dashboard/admin password.
5. Whether to enable token-spending background loops.

Prompts read from `/dev/tty`, so they work even when the script itself arrives through `curl | bash`. If no TTY exists, set environment variables and run non-interactively.

## Prepare configuration without starting services

From a checkout, run:

```bash
./setup.sh --configure-only
# Review .env, then start when ready:
./run.sh -d --build
```

This writes `.env` and `run.sh`, including the correct host bind path, without
installing Docker or starting/stopping services. Python 3 is needed for setup;
no Python packages need to be installed on the host.

To edit an existing setup, use `./setup.sh --configure-only --reconfigure`.
Saved provider, identity, credentials and feature choices become the wizard's
defaults. Explicit exported environment variables take precedence for unattended
updates. Without `--reconfigure`, an existing `.env` is preserved.

## Unattended install

```bash
MAXWELL_NONINTERACTIVE=1 \
MAXWELL_INSTALL_DIR="$HOME/maxwell" \
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

The bootstrap installer needs Git, curl, and Python 3 on the host.

| Requirement | Notes |
|---|---|
| OS | Debian/Ubuntu, Fedora/RHEL, Arch, or macOS with Docker Desktop. |
| Docker | Engine + Compose. Linux uses host networking (`docker-compose.yml`); macOS/Windows uses `docker-compose.bridge.yml`. |
| Python on the host | Needed to write `.env` during install. Maxwell itself runs in the image (Python 3.12). Local development/tests also support Python 3.11; requirements select NumPy 2.3.x there, and NumPy 2.5.1+ on Python 3.12+. |
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
DISCORD_BOT_TOKEN=your-discord-bot-token
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

The dashboard/API starts in the same container on port 8765. Set `MAXWELL_START_API=0` to run only the bot. A supervisor stops both processes if either exits, allowing Docker to restart the complete service. Shutdown forwards signals to both processes, with a bounded grace period. The bundled Linux Docker CLI is used on every host; a host `/usr/bin/docker` mount is no longer needed.

## Credentials and provider setup

### Discord bot token

Create an application at `https://discord.com/developers/applications`, add a Bot, and copy the bot token into `DISCORD_BOT_TOKEN`. Enable the privileged intents **Message Content**, **Server Members**, and **Presence**. Invite the bot with a normal add (no Administrator). Grant it a role in Server Settings if you want mod tools (Manage Channels, Kick/Ban, Moderate Members, and so on). `/install` never requests Administrator.

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

Linux uses `network_mode: host` so `localhost` Ollama, site backends on `127.0.0.1:8800-8899`, and the dashboard on `:8765` work as before. macOS/Windows use `docker-compose.bridge.yml`; the installer rewrites only local outbound service addresses to `host.docker.internal`, leaving passwords, URL paths, and unrelated settings unchanged. It sets the API listener to `0.0.0.0` inside the container; Compose publishes it on host loopback only. For a manual bridge install, set `MAXWELL_API_HOST=0.0.0.0` in `.env` as well.

## Running Maxwell

```bash
cd ~/maxwell
./run.sh -d
docker compose logs -f maxwell
docker compose exec maxwell python3 doctor.py
docker compose down
```

Dashboard: `http://127.0.0.1:8765`.

For reverse proxying, adapt [`examples/Caddyfile.example`](../examples/Caddyfile.example). It serves the dashboard and authenticated `/api/*` and `/data/*` routes on `admin.maxwell.example.com`, and generated sites plus their public backends on `maxwell.example.com`. Replace both example hostnames and configure DNS/TLS for each.

Set `MAXWELL_PUBLIC_BASE_URL` to the generated-site origin and `DISCORD_REDIRECT_BASE` to the dashboard origin. If using Discord OAuth, register `https://<dashboard-host>/api/auth/discord/callback` and set `DISCORD_REDIRECT_URI` accordingly. Set `MAXWELL_CORS_ORIGIN` to the dashboard origin if accessing the admin API cross-origin.

To gate who can add the official bot, serve `/install` on the dashboard origin and set Discord Developer Portal → Installation → Install Link to **Custom URL** `https://<dashboard-host>/install`. Visitors must log in with Discord and be in `admins.json` / `MAXWELL_OWNER_IDS` before Maxwell hands them Discord's Add App URL.

Do not serve generated HTML/JavaScript on the dashboard origin. It would share browser storage with dashboard credentials. Existing installations using one origin must migrate their reverse-proxy configuration; updating Python alone does not isolate static sites. After migration, clear credentials from the old origin and rotate any credentials that may have been exposed.

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

## Development checks

Run core tests in a separate virtual environment without starting Maxwell:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
ruff check .
python -m pytest
```

GitHub Actions runs the same core checks on Python 3.11 and 3.12. Browser,
Riva voice and live-provider checks skip when their dependencies or endpoint
are unavailable. External service behavior still needs testing with your own
configured services.
