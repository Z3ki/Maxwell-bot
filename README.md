# Maxwell

Maxwell is an official Discord AI bot with multimodal chat, tool calling, memory, operator controls, a browser dashboard, background jobs, moderation tools, image generation, web tools, generated sites, and optional second transports. It can use Ollama, OpenRouter, OpenAI, LM Studio, or another OpenAI-compatible API.

## Install — easiest path

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/easy-install.sh | bash
```

The easy installer keeps the first setup small. It asks for your Discord bot token, the AI provider/model, and optionally your Discord user ID for owner controls. It generates a dashboard password, writes a compact `.env`, and then hands off to the Docker installer.

The default installs an unreleased `main` snapshot, pinned to the commit resolved at installation. For production, select a published release; see [versioned installation](docs/INSTALL.md#versioned-installation-and-releases). The initial `0.1.0` release is prepared but not published yet.

Before running it, create a bot in the Discord Developer Portal and enable the privileged gateway intents **Message Content**, **Server Members**, and **Presence**. The setup machine needs Git, curl, and Python 3. Maxwell itself runs in Docker. On supported Linux systems the installer can install Docker; on macOS/Windows use Docker Desktop.

## Friendly AI settings

The easy install uses provider-neutral names:

| Variable | Purpose |
|---|---|
| `AI_API_URL` | OpenAI-compatible API base URL |
| `AI_MODEL` | Main chat model |
| `AI_API_KEY` | API key; blank is normal for local providers that do not require one |
| `DISCORD_BOT_TOKEN` | Official Discord bot token |
| `MAXWELL_OWNER_IDS` | Comma-separated Discord user IDs allowed to use owner/admin controls |
| `MAXWELL_ADMIN_USER` | Dashboard username |
| `MAXWELL_ADMIN_PASSWORD` | Dashboard password |

Example:

```env
DISCORD_BOT_TOKEN=your-discord-bot-token
AI_API_URL=https://openrouter.ai/api/v1
AI_MODEL=moonshotai/kimi-k2.6:free
AI_API_KEY=your-api-key
MAXWELL_OWNER_IDS=123456789012345678
MAXWELL_ADMIN_USER=admin
MAXWELL_ADMIN_PASSWORD=change-me
```

The runtime reads `AI_*` directly. `.env.simple.example` also supplies `OLLAMA_*` interpolation aliases for older tooling. Friendly endpoint/model settings take precedence; an explicitly blank `AI_API_KEY` clears a legacy credential. Avoid conflicting settings.

For an older `.env`, run:

```bash
python3 scripts/migrate_ai_env.py .env
```

That adds the friendly `AI_API_URL`, `AI_MODEL`, and `AI_API_KEY` settings while preserving compatibility aliases.

## Provider examples

### Local Ollama

```env
AI_API_URL=http://localhost:11434
AI_MODEL=qwen3:8b
AI_API_KEY=
```

The easy installer can offer to install Ollama on Linux if it is missing.

### OpenRouter

```env
AI_API_URL=https://openrouter.ai/api/v1
AI_MODEL=moonshotai/kimi-k2.6:free
AI_API_KEY=your-openrouter-key
```

### OpenAI

```env
AI_API_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_API_KEY=your-openai-key
```

### LM Studio

```env
AI_API_URL=http://localhost:1234/v1
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

Dashboard/API: `http://127.0.0.1:8765`

To update:

```bash
cd ~/maxwell
bash install.sh --dir "$PWD"
```

The installer preserves `.env`, `data/`, and generated-site files.

For a published release, use `bash install.sh --dir "$PWD" --version vX.Y.Z` instead. Updates use a detached immutable checkout, so `git pull` is not the update mechanism. The installer refuses tracked local changes rather than overwriting them.

## Discord app commands

Maxwell uses Discord slash commands. `/help` browses commands by topic; `/config` opens a private settings menu for personal defaults and authorized server controls; `/personality` also edits your reply-style preference. Focused commands include `/image`, `/chess`, `/checkers`, `/moderation`, `/memory`, and `/reminder`. `/diagnostics` and `/maintenance` provide separate, developer-restricted operator functions.

The personal-app `/maxwell` command and context-menu actions are available when user install is enabled. `/maxwell` text replies are rendered as Discord embeds. Fast app-command responses stay in the deferred interaction; if a command uses a tool or is still running after about 10 seconds, Maxwell leaves a stable `working on it…` status and sends the final answer as a follow-up.
Private `/maxwell` replies, when selected, are sent only as ephemeral interaction follow-ups; `send_message` can answer privately, but cannot target another chat from a private request.
`/maxwell` replies are public by default. Choose `visibility: Private` for one request, or open `/config` → **Default visibility** → **Private** to save that choice. Explicit command options override saved preferences, including the number of recent channel messages to read. `/config` itself always opens privately, with a settings overview, explanations, marked current choices, reset controls, and a Close button.

The public website includes `/guide/`, `/contact/`, `/terms/`, and `/privacy/` with shared styles and the Maxwell cat icon under `/assets/`. Copy the full `web/` contents when updating the website and update the public route allowlist as shown in `examples/Caddyfile.example`. Keep `/admin/` on the separate operator origin.

Developer access is determined by configured Maxwell owner IDs. Diagnostics redact secret-like values, and maintenance refuses to edit secret controls.

## Advanced install

For the full configuration wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

The advanced `.env.example` still uses the historical `OLLAMA_*` provider names because many optional settings are grouped under that namespace. They are compatibility names for the primary OpenAI-compatible chat provider, not a requirement to use Ollama.

Manual Docker setup:

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
cp .env.simple.example .env
nano .env
bash install.sh --local
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for Docker Desktop, reverse-proxy, OAuth, migration, and unattended-install details.

## Main features

- Discord text, images, audio, video, attachments, embeds, replies, and message context.
- OpenAI-compatible provider support with retry/fallback routing and optional separate background/vision providers.
- Native OpenAI-style tool calls when supported, with a compatibility fallback path.
- Plugin-driven tools and extras: bundled features live under `plugins/`, loaded by `maxwell_core`.
- Tools for web/search, files/media, Discord management, moderation, polls, generated sites, image generation, shell sandboxing, coding/background jobs, and more.
- Fail-closed handling for destructive tools after fetched/web content has tainted the current turn.
- Personal and server settings through `/config` and `/personality`; restricted operator controls through `/diagnostics` and `/maintenance`.
- SQLite-backed RAG/vector memory plus scoped context, entity/knowledge-graph memory, and optional REM-style consolidation.
- Optional autonomy/background actions with runtime controls.
- Generated static sites and optional backend containers.

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
api/api_server.py       Dashboard/admin API
web/                    Dashboard frontend
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

The Maxwell container has access to the host Docker daemon for sandbox/site-container features. Treat that service as host-root trusted even though the main container drops capabilities and uses `no-new-privileges`. Read [`SECURITY.md`](SECURITY.md) before exposing the dashboard or enabling powerful tools on a public server.

## Troubleshooting

Run:

```bash
cd ~/maxwell
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
docker compose logs -f maxwell
```

If the Discord token is rejected, regenerate/copy the **bot token from the Discord Developer Portal**. Do not copy an authorization header or user token from a logged-in Discord client.
