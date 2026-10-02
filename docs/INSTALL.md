# Installing Maxwell

Maxwell's supported runtime is Docker. The host needs Git, curl, Python 3 for setup, and Docker Engine + Compose (or Docker Desktop). Maxwell's Python/runtime dependencies live inside the container.

## Fastest path: easy installer

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/easy-install.sh | bash
```

The easy installer clones/updates Maxwell, asks for the minimum useful configuration, writes a compact `.env`, generates the operator API password, and hands off to the normal Docker installer.

It asks for:

1. An official Discord bot token from the Discord Developer Portal.
2. The AI provider/model and API key if the provider needs one.
3. An optional Discord owner user ID for restricted `/diagnostics` and `/maintenance` commands.

The easy config uses `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY`, read directly by the runtime. Existing historical `OLLAMA_*` variables and `AI_API_URL` remain accepted. An explicitly blank friendly key clears a legacy credential.

Before starting, enable the privileged Discord gateway intents **Message Content**, **Server Members**, and **Presence**.

## Versioned installation and releases

The default command installs an unreleased `main` development snapshot. Both installers resolve the selected branch/tag once and use that immutable commit for the app, setup scripts, and configuration handoff. The checkout is detached; do not use `git pull` to update it.

For version `0.1.4`, select the exact tag `v0.1.4`:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/v0.1.4/easy-install.sh -o /tmp/maxwell-install.sh
bash /tmp/maxwell-install.sh --version v0.1.4
```

To also prepare the public shell sandbox on that Linux Docker host, pass
`--with-shell`. It installs gVisor and the host firewall, and it restarts
Docker when the storage driver has to change. Hosts that should not run
shell can omit the flag; Maxwell then keeps shell disabled.

See [GitHub Releases](https://github.com/Z3ki/Maxwell-bot/releases) for release availability and assets. Changing `VERSION` alone does not publish a release tag. Exact full commit IDs are also supported with `--ref <40-character-sha>`. `--version` and `--ref` are mutually exclusive. Use `--dir <path>` to select the installation directory.

For a fork, set `MAXWELL_REPO_URL`; `MAXWELL_VERSION` / `MAXWELL_REF` are the environment equivalents. `MAXWELL_BRANCH` selects development snapshots only. A missing/invalid revision or incomplete checkout fails before replacing an existing installation. Tracked local changes must be committed or saved first; `.env`, memory, generated sites, and other persistent untracked runtime files are retained.

Maintainers: update `VERSION` and `CHANGELOG.md`, verify the checkout, then push a matching semantic tag (`vMAJOR.MINOR.PATCH`). The release workflow rejects tag/version mismatches, runs checks, builds and smokes the real Docker entrypoint, publishes source/installers with checksums, and pushes a matching GHCR image. `CONTAINER_IMAGE` records the image digest. No moving `latest` tag is published. This repository runs from a checkout; it does not publish a Python wheel.


## Full installer

Use the full wizard when you want advanced settings during installation:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

The full wizard writes `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY`, plus `OLLAMA_*` compatibility aliases. Those names do not mean Maxwell requires Ollama; OpenRouter, OpenAI, LM Studio, and other compatible endpoints work too.

The full installer can configure:

1. Discord bot token.
2. Primary provider URL/model/key.
3. Identity and owner IDs.
4. Operator API credentials.
5. Optional autonomy/REM background features.

Prompts read from `/dev/tty`, so an interactive `curl | bash` install still works. When there is no controlling TTY, the installer switches to non-interactive mode and reads environment variables.

## Requirements

| Requirement | Notes |
|---|---|
| OS | Linux is the primary target. macOS/Windows use Docker Desktop and the bridge Compose file. |
| Docker | Docker Engine + Compose, or Docker Desktop. |
| Git/curl | Needed to fetch/update the checkout. |
| Python 3 | Needed by setup scripts that write `.env`. Maxwell itself runs in Docker. |
| Network | GitHub, image/package registries used during the build, Discord, and your AI endpoint. |
| Resources | The bot itself is modest; local LLMs can require much more RAM/disk/GPU. |

On supported Linux package managers the installer can install Docker. On macOS/Windows install/start Docker Desktop first.

## Prepare configuration without starting services

From an existing checkout:

```bash
./setup.sh --configure-only
```

Review `.env`, then start:

```bash
./run.sh -d --build
```

To re-run the full wizard for an existing checkout:

```bash
./install.sh --local --reconfigure
```

Without `--reconfigure`, an existing `.env` is preserved.

## Unattended install

For the full installer, export the values it understands and run non-interactively:

```bash
MAXWELL_NONINTERACTIVE=1 \
MAXWELL_INSTALL_DIR="$HOME/maxwell" \
DISCORD_BOT_TOKEN="your-discord-bot-token" \
AI_BASE_URL="https://openrouter.ai/api/v1" \
AI_MODEL="moonshotai/kimi-k2.6:free" \
AI_API_KEY="your-openrouter-key" \
MAXWELL_OWNER_IDS="123456789012345678" \
MAXWELL_ADMIN_PASSWORD="change-me" \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh)"
```

To install from a fork/mirror, set `MAXWELL_REPO_URL` and optionally `MAXWELL_BRANCH`.

For CI/sandboxes where Docker must not be installed by the script, set `MAXWELL_SKIP_SYSTEM_DEPS=1` only after Docker Engine + Compose are already usable by the current user.

## Manual install with the friendly config

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
cp .env.simple.example .env
chmod 600 .env
nano .env
bash install.sh --local
```

Minimum friendly config:

```ini
DISCORD_BOT_TOKEN=your-discord-bot-token
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3:8b
AI_API_KEY=
MAXWELL_OWNER_IDS=123456789012345678
MAXWELL_ADMIN_PASSWORD=change-me
```

The installer fills `MAXWELL_HOST_BIND` for Docker sibling-container mounts.

For the complete advanced template, use `.env.example` instead. See [CONFIGURATION.md](CONFIGURATION.md).

## Migrating older provider variables

Existing installations that use only `OLLAMA_*` can be migrated to the friendlier primary names without breaking compatibility:

```bash
cd ~/maxwell
python3 scripts/migrate_ai_env.py .env
```

After migration, humans can edit:

```ini
AI_BASE_URL=...
AI_MODEL=...
AI_API_KEY=...
```

The migration preserves compatibility aliases for older code/tooling. Avoid assigning conflicting values to both namespaces.

## Provider examples

### Ollama

```ini
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3:8b
AI_API_KEY=
```

If RAG embeddings use local Ollama too, configure the embedding endpoint/model in the advanced config and pull that embedding model on the host.

### OpenRouter

```ini
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=moonshotai/kimi-k2.6:free
AI_API_KEY=your-openrouter-key
```

### OpenAI

```ini
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_API_KEY=your-openai-key
```

### LM Studio

```ini
AI_BASE_URL=http://localhost:1234/v1
AI_MODEL=the-loaded-model-name
AI_API_KEY=
```

A bare local host such as `http://localhost:11434` is normalized by the provider layer to the compatible `/v1` API. URLs that already include a path such as `/api/v1` or `/v1` are used as configured.

## Discord setup

Create an application at the Discord Developer Portal, add a **Bot**, copy its **bot token**, and enable **Message Content**, **Server Members**, and **Presence** intents.

Use Guild Install when adding Maxwell to a server. Maxwell does not require the Administrator permission by default. Grant only the Discord permissions needed for the moderation/management tools you intend to use.

Ask Maxwell for an invite link to a server it is already in by naming the server or giving its ID. `create_invite` selects a usable text channel and requires the requesting user and Maxwell to have `create_instant_invite` in that target server. Maxwell cannot join a server from a `discord.gg` invite; use the OAuth add-server link for that.

User Install enables the personal `/maxwell` command and context-menu actions. `/config` opens a private settings menu for personal defaults; server controls appear only to authorized server managers and check permissions again when used. `/diagnostics` and `/maintenance` are restricted to configured Maxwell developer IDs.

Never use a Discord user/self-bot token. Never copy a browser `Authorization` header into Maxwell.

## App-command behavior

`/help` browses slash commands by topic. `/config` opens a private settings menu for personal defaults and authorized server settings; `/personality` also edits your reply-style preference. Purpose-specific commands include `/image`, `/chess`, `/checkers`, `/moderation`, `/memory`, and `/reminder`. Restricted runtime status and operational controls are split between `/diagnostics` and `/maintenance`.

For `/maxwell`, fast answers remain in the original deferred interaction. If the command invokes a tool or remains unanswered after roughly 10 seconds, Maxwell keeps the working status and replies to it when channel replies are available; otherwise it edits the original status when possible or sends an interaction follow-up. Textual `/maxwell` replies are rendered as embeds.

Maxwell does not run `web_search` before generation. It searches when the user asks for a lookup or the answer needs a live external fact, and it does not append source links unless a result is used. `/maxwell` supports `web=search` to ask for a lookup and `web=off` to disable web tools for the turn. Treat results as untrusted evidence.

## Docker behavior

Linux uses `docker-compose.yml` with host networking. This keeps host-local Ollama, Discord voice UDP, generated-site backends, and the operator API reachable without extra port mapping.

Docker Desktop uses `docker-compose.bridge.yml`. The installer rewrites local service addresses to `host.docker.internal` where appropriate and publishes the admin API on host loopback. For a manual bridge install, make sure `MAXWELL_API_HOST=0.0.0.0` is set inside the container configuration.

The Maxwell service mounts the Docker socket so shell sandboxes and generated-site backends can run as sibling containers. This is a host-root trust boundary; read [../SECURITY.md](../SECURITY.md).

## Running Maxwell

```bash
cd ~/maxwell
./run.sh -d
```

Then:

```bash
docker compose logs -f maxwell
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```

Operator API: `http://127.0.0.1:8765/api`, authenticated with HTTP Basic using `MAXWELL_ADMIN_USER` and `MAXWELL_ADMIN_PASSWORD`. Use Discord `/config` and authorized `/maintenance` controls for interactive settings; there is no browser admin frontend.

Stop:

```bash
docker compose down
```

Rebuild/start:

```bash
./run.sh -d --build
```

## Reverse proxy and public generated sites

Use [`examples/Caddyfile.example`](../examples/Caddyfile.example) as the reference for a split-origin deployment.

The public root is static: landing page, guide, contact, terms, privacy, and assets. Copy the contents of `web/` into `/var/www/maxwell` when using the example. Generated sites stay under `/var/www/maxwell/bot`; the authenticated operator API is available only on a separate API hostname. Updating static files does not require restarting the bot.

When upgrading an existing deployment, remove stale admin frontend files from the deployed static root, including any `admin/` directory; copying the current `web/` files does not delete old files. Keep `/admin` and `/admin/*` denied on public routes.

Keep generated HTML/JavaScript on a different origin from the operator API. Set `MAXWELL_PUBLIC_BASE_URL` to the generated-site origin. Set `MAXWELL_CORS_ORIGIN` only if an authorized API client requires cross-origin access.

The operator API uses HTTP Basic authentication with `MAXWELL_ADMIN_USER` and `MAXWELL_ADMIN_PASSWORD`, not Discord browser OAuth. Do not serve arbitrary generated pages from the API origin: generated JavaScript sharing an origin can access browser credentials for that origin.

## Updating

```bash
cd ~/maxwell
bash install.sh --dir "$PWD"
```

The installer keeps `.env`, `data/`, and generated-site state.

To update to a published release, add `--version vX.Y.Z`. The installer resolves and validates the revision before checkout. Back up `.env`, `data/`, and generated sites before an operational update; the installer preserves those files but a backup is still required for recovery.

Reconfigure advanced settings:

```bash
./install.sh --local --reconfigure
```

## Migrating from an old host/venv/PM2 deployment

Do not run the old host process and Docker Maxwell at the same time with the same Discord bot token.

From the checkout:

```bash
bash install.sh --dir "$PWD"
```

The supported runtime is Docker. Historical PM2/host files may remain in the repository for compatibility/migration, but they are not the recommended production path.

## Email integration

Email tools use the SMTP/IMAP settings documented in [`../email_integration/README.md`](../email_integration/README.md). The Cloudflare `setup_dns.py` helper is an optional DNS utility; it does not configure Maxwell's local SMTP/IMAP transport.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker info` fails | Start Docker. On Linux, make sure your user can access the daemon and re-login after joining the `docker` group. |
| Compose cannot find/build the image | Run `./run.sh -d --build` from the Maxwell checkout. |
| Shell/site sibling bind mounts fail | Verify `MAXWELL_HOST_BIND` points to the absolute host checkout path. |
| `doctor.py --probe` returns 404 | Check the provider base URL and model. Local Ollama should normally be `http://localhost:11434`; hosted compatible APIs usually include their documented `/v1` path. |
| `doctor.py --probe` returns 401/403 | Check the provider API key and account/model access. |
| Local Ollama is unreachable on Docker Desktop | Use the installer/bridge setup so local addresses are rewritten to `host.docker.internal`. |
| Discord token is invalid | Copy or regenerate the **bot token** in the Discord Developer Portal. Do not use a browser authorization header or user token. |
| `curl | bash` prompts do not appear | Run in an interactive terminal with `/dev/tty`, or configure the full installer non-interactively with environment variables. |
| Operator API returns 503 | Set `MAXWELL_ADMIN_PASSWORD` and restart the stack. |

## Development checks

For local code changes outside Docker:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
ruff check .
python -m pytest
```

GitHub Actions runs the repository's configured lint/test matrix. Live external-service behavior still requires the corresponding services/credentials and should not be inferred from mocked unit tests alone.
