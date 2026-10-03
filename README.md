# Maxwell

Maxwell is an official Discord AI bot with multimodal chat, tool calling, memory, operator controls, background jobs, moderation tools, image generation, web tools, generated sites, and optional second transports. It can use Ollama, OpenRouter, OpenAI, LM Studio, or another OpenAI-compatible API.

## Install — easiest path

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/easy-install.sh | bash
```

The easy installer keeps the first setup small. It asks for your Discord bot token, the AI provider/model, and optionally your Discord user ID for owner controls. It generates an operator API password, writes a compact `.env`, and then hands off to the Docker installer.

The default installs an unreleased `main` snapshot, pinned to the commit resolved at installation. For version `0.1.6`, select `--version v0.1.6`; see [versioned installation](docs/INSTALL.md#versioned-installation-and-releases) and [GitHub Releases](https://github.com/Z3ki/Maxwell-bot/releases). Add `--with-shell` on a Linux Docker host that should run the public shell sandbox.

Before running it, create a bot in the Discord Developer Portal and enable the privileged gateway intents **Message Content**, **Server Members**, and **Presence**. The setup machine needs Git, curl, and Python 3. Maxwell itself runs in Docker. On supported Linux systems the installer can install Docker; on macOS/Windows use Docker Desktop.

## Friendly AI settings

The easy install uses provider-neutral names:

| Variable | Purpose |
|---|---|
| `AI_BASE_URL` | OpenAI-compatible API base URL |
| `AI_MODEL` | Main chat model |
| `AI_API_KEY` | API key; blank is normal for local providers that do not require one |
| `DISCORD_BOT_TOKEN` | Official Discord bot token |
| `MAXWELL_OWNER_IDS` | Comma-separated Discord user IDs allowed to use owner/admin controls |
| `MAXWELL_ADMIN_USER` | Operator API username |
| `MAXWELL_ADMIN_PASSWORD` | Operator API password |

Example:

```env
DISCORD_BOT_TOKEN=your-discord-bot-token
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=moonshotai/kimi-k2.6:free
AI_API_KEY=your-api-key
MAXWELL_OWNER_IDS=123456789012345678
MAXWELL_ADMIN_USER=admin
MAXWELL_ADMIN_PASSWORD=change-me
```

The runtime reads `AI_*` directly. Legacy `OLLAMA_*` variables and `AI_API_URL` remain supported. Friendly endpoint/model settings take precedence; an explicitly blank `AI_API_KEY` clears a legacy credential. Avoid conflicting settings.

For an older `.env`, run:

```bash
python3 scripts/migrate_ai_env.py .env
```

That adds the friendly `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY` settings while preserving compatibility aliases.

## Provider examples

### Local Ollama

```env
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3:8b
AI_API_KEY=
```

The easy installer can offer to install Ollama on Linux if it is missing.

### OpenRouter

```env
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=moonshotai/kimi-k2.6:free
AI_API_KEY=your-openrouter-key
```

### OpenAI

```env
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_API_KEY=your-openai-key
```

### LM Studio

```env
AI_BASE_URL=http://localhost:1234/v1
AI_MODEL=local-model
AI_API_KEY=
```

Use the model name LM Studio is actually serving.

## Run and update

The default install location is `~/maxwell`.

```bash
cd ~/maxwell
./run.sh -d
```

Useful commands:

```bash
# Follow logs
docker compose logs -f maxwell

# Check config/dependencies
docker compose exec maxwell python3 doctor.py

# Probe configured AI/embedding endpoints
docker compose exec maxwell python3 doctor.py --probe

# Stop Maxwell
docker compose down

# Rebuild/start again
./run.sh -d --build
```

Authenticated operator API: `http://127.0.0.1:8765`. Administration is available through Discord `/config`; there is no web admin dashboard.

To update:

```bash
cd ~/maxwell
bash install.sh --dir "$PWD"
```

The installer preserves `.env`, `data/`, and generated-site files.

For a published release, use `bash install.sh --dir "$PWD" --version vX.Y.Z` instead. Updates use a detached immutable checkout, so `git pull` is not the update mechanism. The installer refuses tracked local changes rather than overwriting them.

## Discord app commands

Maxwell uses Discord slash commands. `/help` browses commands by topic; `/config` opens a private settings menu with direct buttons for **Personality**, **Language**, and **Reply visibility**. **More options** contains web search, answer detail, channel context, response mode, and your own API key. Authorized members can switch to server controls, and configured application owners can access global controls and diagnostics.

The personal-app `/maxwell` command and context-menu actions are available when user install is enabled. `/maxwell` and message actions answer with normal Discord text, briefly by default. Select Balanced or Deep when you want more explanation. Message → Apps → **Ask Maxwell** responds directly to the selected message without opening a form. **Rewrite / Translate** opens a form for your prompt, action, detail, visibility, and language; Summarize, Explain, and Fact-check remain quick actions. All entry points use your saved preferences. In DMs, the app includes observed participants, selected-message reply chains, attachments, buttons, embeds, forwarded messages, and live history when Discord permits access. Private DM requests can use this same-chat history; other DMs and server history are not borrowed. Fast app-command responses stay in the deferred interaction; if a command uses a tool or is still running after about 10 seconds, Maxwell leaves a stable `working on it…` status and sends the final answer as a follow-up.
Private `/maxwell` replies, when selected, are sent only as ephemeral interaction follow-ups; `send_message` can answer privately, but cannot target another chat from a private request.
`/maxwell` replies are public by default. Choose `visibility: Private` for one request, or open `/config` → **Reply visibility** → **Private** to save that choice. Explicit command options override saved preferences. Channel context offers 0, 10, 25, 50, 100, 250, 500, or 1,000 recent messages through the `context` option on `/maxwell` and `/config` → **More options** → **Channel context**. Maxwell must be able to read the channel, and very long histories are trimmed to fit the model. Your saved personality and language apply to your replies across channels, servers, and DMs, including ordinary messages, mentions, slash commands, and context-menu requests. They are isolated by Discord user ID and do not change Maxwell's shared identity or permissions. `/config` always opens privately, with a compact embed, marked current choices, reset controls, and a Close button.

The public website includes `/guide/`, `/contact/`, `/terms/`, and `/privacy/` with shared styles and the Maxwell cat icon under `/assets/`. Copy the full `web/` contents when updating the website and update the public route allowlist as shown in `examples/Caddyfile.example`. The browser admin dashboard and its Discord OAuth login have been removed. Remove any previously deployed `admin/` static files; keep the authenticated operator API on a separate origin.

Developer access is determined by configured Maxwell owner IDs. Diagnostics redact secret-like values, and maintenance refuses to edit secret controls.

Maxwell can inspect its running revision, changelog, recent public GitHub commits, and changed-file summaries through `get_maxwell_updates`. The running revision is captured at startup; newer commits on `main` are not assumed to be deployed. GitHub failures fall back to the local snapshot.

## Advanced install

For the full configuration wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

Both environment templates use provider-neutral `AI_*` names. Legacy `OLLAMA_*` variables and `AI_API_URL` remain deprecated input aliases. Ollama remains fully supported through its OpenAI-compatible endpoint. See [provider architecture and migration](docs/PROVIDERS.md).

Manual Docker setup:

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
cp .env.simple.example .env
nano .env
bash install.sh --local
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for Docker Desktop, reverse-proxy, OAuth, migration, and unattended-install details.

### Custom site backends

Generated sites support real custom Python backends through `site_server`: API routes, authentication, WebSockets, external services, and server-side secrets. `create_site(backend=true)` still enables only the built-in KV API; use `site_server` for application logic. New applications use one shared Python 3.12 + FastAPI/Uvicorn image, one worker with no reload, and SQLite at `/data/app.db`. Frontend requests use relative `api/...` URLs. Put credentials in backend environment variables, never in public HTML/JavaScript. Backend source, logs, environment, and lifecycle are restricted to the site owner or an application admin.

Before deploying backends, run `sudo bash scripts/setup_site_host.sh` on the Docker host and apply the Compose configuration that mounts `/etc/maxwell-sites` read-only at `/run/maxwell-sites-policy`. The dedicated site pool is limited to **2 CPUs / 4 GiB** in aggregate; each site has a **256 MiB / 0.5 CPU** limit, with at most **64** backend sites. Host setup is required: backend deployment fails closed without the resource policy. Existing Flask applications and `/data` databases remain supported and are not rewritten or deleted; existing containers need an operator-managed restart into the pool.


## Main features

- Discord text, images, audio, video, attachments, embeds, replies, and message context.
- OpenAI-compatible provider support with retry/fallback routing and optional separate background/vision providers.
- Native OpenAI-style tool calls when supported, with a compatibility fallback path.
- Plugin-driven tools and extras: bundled features live under `plugins/`, loaded by `maxwell_core`.
- Tools for web/search, files/media, Discord management, moderation, polls, generated sites, image generation, shell sandboxing, coding/background jobs, and more.
- Fail-closed handling for destructive tools after fetched/web content has tainted the current turn.
- Personal, server, and restricted application-owner settings through `/config`.
- SQLite-backed RAG/vector memory plus scoped context, entity/knowledge-graph memory, and optional REM-style consolidation.
- Optional autonomy/background actions with runtime controls.
- Generated static sites and custom FastAPI/SQLite backend containers with server-side secrets and aggregate resource limits.

- Docker-first runtime so host Python/package versions do not fight the bot.

## Project layout

```text
bot.py                  Discord transport and turn loop
maxwell_core/           Plugin host, tool registry, prompts, hooks
plugins/                Feature plugins (tools, jobs, prompt slices)
bot_tools.py            Compatibility re-exports of plugin tools
providers.py            OpenAI-compatible provider wrapper
config.py               Environment-backed config
rag_memory.py           Vector/RAG memory
knowledge_graph.py      Entity/relationship memory
jobs.py                 Background jobs
control_defaults.py     Runtime control defaults
user_install.py         Discord user-install/app-command support
plugin_manager.py       Compatibility façade over maxwell_core
api/api_server.py       Operator and generated-site APIs
web/                    Public landing, guide, contact, and policy pages
doctor.py               Install/config checker
easy-install.sh         Three-step installer
install.sh              Full installer
.env.simple.example     Small human-friendly config
.env.example            Full advanced config
docker-compose.yml      Linux Docker runtime
docs/PLUGINS.md         Plugin development API
```

## Documentation

- [Installation](docs/INSTALL.md)
- [Configuration](docs/CONFIGURATION.md)
- [Architecture overview](docs/OVERVIEW.md)
- [Plugin development](docs/PLUGINS.md)
- [Security](SECURITY.md)
- [Discord persona](docs/MAXWELL_PERSONA.md)
- [Audit snapshot](docs/AUDIT.md)
- [Changelog and release status](CHANGELOG.md)
- [GitHub projects](docs/GITHUB_PROJECTS.md)
- [Agent life](docs/LIVING_AGENT.md)
- [Email integration](email_integration/README.md)

`CONTEXT_MEMORY_ANALYSIS.md` and `RELIABILITY_RESEARCH.md` are retained as historical/research entry points, but their current-status sections replace obsolete architecture claims and point back to the live implementation/docs.

## Security notes

Keep `.env` and `data/` private. They may contain Discord tokens, API keys, credentials, private context, and operational state. Never use a Discord user/self-bot token; Maxwell expects an official bot token from the Developer Portal.

The Maxwell container has access to the host Docker daemon for sandbox/site-container features. Treat that service as host-root trusted even though the main container drops capabilities and uses `no-new-privileges`. Read [`SECURITY.md`](SECURITY.md) before exposing the operator API or enabling powerful tools on a public server.

## Troubleshooting

Run:

```bash
cd ~/maxwell
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
docker compose logs -f maxwell
```

If the Discord token is rejected, regenerate/copy the **bot token from the Discord Developer Portal**. Do not copy an authorization header or user token from a logged-in Discord client.
