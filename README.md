# Maxwell

Maxwell is a Discord AI bot with multimodal chat, tool calling, memory, a web dashboard, background jobs, moderation/admin tools, image generation, web tools, site generation, and more. It can use Ollama, OpenRouter, OpenAI, LM Studio, or basically any OpenAI-compatible API.

## Install — easy way

Run this:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/easy-install.sh | bash
```

The easy installer asks for only three things:

1. Your Discord bot token.
2. The AI provider/model you want Maxwell to use.
3. Your Discord user ID if you want owner/admin access.

It generates the dashboard password, writes a small `.env`, keeps the advanced defaults out of your face, and then hands off to the normal Docker installer.

> **good luck installing this shit!**

### Before you run it

Create a bot at the Discord Developer Portal and enable these privileged gateway intents:

- Message Content
- Server Members
- Presence

The setup machine needs Git, curl, and Python 3. Maxwell itself runs in Docker. The normal installer can install Docker on supported Linux systems; on macOS/Windows, install Docker Desktop if needed.

## Better environment variable names

The simple install uses provider-neutral names instead of pretending every AI provider is Ollama:

| Variable | What it means |
|---|---|
| `AI_API_URL` | Base URL for the AI API |
| `AI_MODEL` | Main chat model name |
| `AI_API_KEY` | API key, blank for local providers that do not require one |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `MAXWELL_OWNER_IDS` | Comma-separated Discord IDs allowed to use owner/admin controls |
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

Maxwell still keeps compatibility aliases for the older `OLLAMA_*` names internally, so existing installs do not have to break just because the public config got cleaned up.

### Existing install? Rename the old variables safely

From your Maxwell checkout:

```bash
python3 scripts/migrate_ai_env.py .env
```

That converts the main provider settings to:

```env
AI_API_URL=...
AI_MODEL=...
AI_API_KEY=...
```

and leaves compatibility aliases behind for old code/tooling.

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

Use whatever model name LM Studio is actually serving.

## Running Maxwell

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

# Stop Maxwell
docker compose down

# Start/update containers again
./run.sh -d --build
```

The dashboard/API is available locally at:

```text
http://127.0.0.1:8765
```

## Reconfigure or update

Edit the friendly config directly:

```bash
nano ~/maxwell/.env
```

Then restart:

```bash
cd ~/maxwell
./run.sh -d
```

Update Maxwell:

```bash
cd ~/maxwell
git pull --ff-only
bash install.sh
```

The installer preserves `.env` and `data/`.

## Advanced install

If you want the full configuration wizard instead of the three-step setup:

```bash
curl -fsSL https://raw.githubusercontent.com/Z3ki/Maxwell-bot/main/install.sh | bash
```

Manual Docker setup:

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell
cd maxwell
cp .env.simple.example .env
nano .env
bash install.sh
```

The giant `.env.example` is still available for every advanced option.

## Main features

- Discord text, images, audio, video, attachments, embeds, and message context.
- OpenAI-compatible AI provider support instead of being locked to one vendor.
- Tool calling for web/search, files, media, Discord management, moderation, polls, sites, image generation, shell sandboxing, coding/background jobs, and more.
- Owner/admin controls and a browser dashboard.
- RAG/vector memory plus optional REM-style background memory consolidation.
- Autonomy/background actions with runtime controls.
- Generated static sites and optional backend containers.
- Telegram as an optional second transport.
- Docker-first runtime so host Python/package versions do not fight the bot.

## Project layout

```text
bot.py                  Main Discord bot
bot_tools.py            Tool implementations
providers.py            OpenAI-compatible provider wrapper
config.py               Environment-backed config
rag_memory.py           Vector/RAG memory
jobs.py                 Background jobs
control_defaults.py     Runtime control defaults
api/api_server.py       Dashboard/admin API
web/                    Dashboard frontend
doctor.py               Install/config checker
easy-install.sh         Three-step installer
install.sh              Full installer
.env.simple.example     Small human-friendly config
.env.example            Full advanced config
docker-compose.yml      Linux Docker runtime
```

## More documentation

- [Installation details](docs/INSTALL.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Architecture overview](docs/OVERVIEW.md)

## Security notes

Keep `.env` private. It may contain Discord tokens, API keys, and dashboard credentials. Do not commit it.

Owner/admin access should be restricted with `MAXWELL_OWNER_IDS`. Review optional features before exposing the dashboard or giving a model access to powerful tools on a public server.

## Troubleshooting

Run:

```bash
cd ~/maxwell
docker compose exec maxwell python3 doctor.py
```

For a deeper provider check:

```bash
docker compose exec maxwell python3 doctor.py --probe
```

And inspect logs with:

```bash
docker compose logs -f maxwell
```

If the easy installer is not enough, use the full guide in [`docs/INSTALL.md`](docs/INSTALL.md).
