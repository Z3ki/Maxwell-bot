# Maxwell

Maxwell is a Discord self-bot backed by any OpenAI-compatible API. It reads text, images, audio, video, file attachments, and Discord embeds, then responds using an LLM with tool-calling support. It includes a web dashboard, admin API, and temporary site generation.

**This is a self-bot** (`discord.py-self`, `self_bot=True`). Self-bots may violate Discord ToS. Use at your own risk.

## Features

- Multimodal input: images, audio, video, text files, and Discord embeds are forwarded to the model with normalized video, extracted frames, and extracted audio.
- Visual memory: recent images persist across messages per channel (configurable depth).
- Tool system: image generation (Pollinations, NVIDIA NIM, GPT-compatible), web search, URL fetch, arbitrary file sending, meme/media sending, shell execution, polls, invites, site generation, avatar/presence/nickname changes, message editing/forwarding/deletion, and more.
- Auto mode: per-channel opt-in where Maxwell decides whether to respond to each message via a lightweight decider prompt.
- Reaction handler: responds to emoji reactions on its own messages in auto-mode channels.
- Per-server custom prompts, long-term memory, and scoped cross-context facts across DMs, servers, groups, and channels.
- Opt-in REM "dreaming" pass that periodically consolidates recent visible traffic into long-term memory.
- Web dashboard with public read-only GET endpoints and auth-protected mutations.
- Temporary site hosting: generates HTML sites served under a configurable public URL.

## Project Structure

```
bot.py              Main bot entry point
bot_tools.py        Tool implementations
providers.py        OpenAI-compatible provider wrapper
config.py           Environment-backed configuration
memory.py           Channel/server memory manager
api/api_server.py   Dashboard and admin API server
web/                Static dashboard files (index.html, admin/)
examples/           Caddyfile and PM2 config examples
docker/             Legacy shell sandbox Dockerfile
ecosystem.config.js PM2 process config
```

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your values, then run:

```bash
python bot.py
python api/api_server.py
```

Or with PM2:

```bash
pm2 start ecosystem.config.js
```

## Environment Variables

See `.env.example` for a full template. Key variables:

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord user token |
| `TELEGRAM_TOKEN` | No | Telegram bot token for optional Telegram long-polling support |
| `OLLAMA_BASE_URL` | No | OpenAI-compatible API base URL (default: `http://localhost:11434`) |
| `OLLAMA_API_KEY` | No | Bearer token for the LLM API (falls back to `OPENAI_COMPAT_API_KEY`) |
| `OLLAMA_MODEL` | No | Model name (default: `gemma4:31b-cloud`) |
| `OLLAMA_REM_MODEL` | No | Optional REM dreamer model (defaults to `OLLAMA_MODEL`) |
| `OLLAMA_MAX_TOKENS` | No | Max tokens (default: 200000) |
| `OLLAMA_TEMPERATURE` | No | Temperature (default: 1.0) |
| `OLLAMA_FALLBACK_BASE_URL` | No | Optional secondary OpenAI-compatible API base URL. Attempts rotate primary/fallback when set. |
| `OLLAMA_FALLBACK_API_KEY` | No | Bearer token for the fallback LLM API. |
| `OLLAMA_FALLBACK_MODEL` | No | Model name for the fallback provider. |
| `OLLAMA_FALLBACK_DISABLE_REASONING` | No | Add OpenRouter-compatible reasoning exclusion on fallback calls (default: `true`). |
| `OLLAMA_RETRY_ATTEMPTS` | No | Total provider attempts per request (default: `3`; with fallback: primary, fallback, primary). |
| `NVIDIA_API_KEY` | No | NVIDIA NIM API key for HD image generation |
| `GPT_IMAGE_URL` | No | GPT-compatible image generation endpoint |
| `GPT_IMAGE_API_KEY` | No | API key for GPT image endpoint |
| `DATA_DIR` | No | Data storage directory (default: `data`) |
| `MAXWELL_ADMIN_USER` | Yes | Admin username for dashboard API |
| `MAXWELL_ADMIN_PASSWORD` | Yes | Admin password for dashboard API |
| `MAXWELL_SITE_DIR` | No | Directory for generated bot sites (default: `public/bot`) |
| `MAXWELL_PUBLIC_BASE_URL` | No | Public URL for generated sites |
| `MAXWELL_API_HOST` | No | API bind address (default: `127.0.0.1`) |
| `MAXWELL_API_PORT` | No | API port (default: `8765`) |
| `MAXWELL_CORS_ORIGIN` | No | Allowed CORS origin (default: same as `MAXWELL_PUBLIC_BASE_URL`) |
| `REM_ENABLED` | No | Enable background REM dreaming (default: `false`) |
| `REM_INTERVAL_SECONDS` | No | REM interval in seconds (default: `600`) |
| `REM_MAX_TURNS` | No | Maximum REM tool-call rounds (default: `3`) |
| `REM_EVENT_BUFFER_MAX` | No | Global visible event buffer cap (default: `500`) |
| `REM_RUN_HISTORY` | No | REM audit history length (default: `50`) |

### Temporary Free Model

For a temporary free OpenRouter fallback, the current recommended model is Moonshot AI Kimi K2.6:

- Model page: `https://openrouter.ai/moonshotai/kimi-k2.6:free`
- `OLLAMA_FALLBACK_BASE_URL=https://openrouter.ai/api/v1`
- `OLLAMA_FALLBACK_MODEL=moonshotai/kimi-k2.6:free`
- `OLLAMA_FALLBACK_DISABLE_REASONING=true`

It is useful as a free temporary fallback, but check OpenRouter for current availability, modality support, and rate limits.

## Commands

All commands use the `,` prefix. Admin commands require the user to be in the admin list.

| Command | Admin | Description |
|---|---|---|
| `,stop` | No | Cancel the active AI request in this channel |
| `,prompt [text]` | Yes | View or set a custom server prompt |
| `,clearprompt` | Yes | Clear the custom server prompt |
| `,clearmem` | Yes | Clear channel memory and all cached state |
| `,auto` | Yes | Toggle auto-mode for this channel |
| `,auto list` | Yes | List all auto-mode channels |
| `,drug [minutes]` | No | Temporary "fried" personality override |
| `,drug off` | No | Turn off drug mode |
| `,blacklist [user]` | Yes | Add/view/clear blacklisted users |
| `,unblacklist [user]` | Yes | Remove a user from the blacklist |
| `,context` | Yes | Show relevant scoped cross-context facts |
| `,context all` | Yes | Show recent shared context facts |
| `,context add [scope] <fact>` | Yes | Manually add a scoped context fact |
| `,context forget <id>` | Yes | Delete a shared context fact |
| `,context private <id>` | Yes | Mark a shared context fact private |
| `,context global <id>` | Yes | Promote a fact to global shared context |
| `,rem` | Yes | Show REM status and last audit preview |
| `,rem now` | Yes | Trigger one REM dream pass immediately |
| `,rem on` / `,rem off` | Yes | Enable or disable REM for this process |
| `,rem audit [N]` | Yes | Show recent REM run audits |
| `,rem fix` | Yes | Restore REM prompt/interval/max-turn defaults |
| `,vc join` | No | Join your current VC and start live listening |
| `,vc leave` | No | Stop listening and disconnect from VC |
| `,vc listen` | No | Start live VC listening while staying connected |
| `,vc unlisten` | No | Stop live VC listening while staying connected |
| `,vc status` | No | Show VC connection/listening and voice settings |
| `,vc say <text>` | No | Speak text in VC with TTS |

Live VC replies require `discord-ext-voice-recv`, `PyNaCl`, `ffmpeg`, and an audio-capable OpenAI-compatible provider.

## Memory and REM

Maxwell keeps its existing memory surfaces: `memory.json` for per-channel short-term chat history and `long_term_memory.txt` for durable line-oriented memory. REM adds a separate visible-only ring at `data/rem_events.json` and, when enabled, periodically "dreams" over events since the previous run.

The dreamer is not a live chat response and never posts to Discord. It sends a bounded short-term slice to the configured OpenAI-compatible provider, consults current long-term memory, and can privately add, edit, search, or remove durable memory lines through `MemoryManager`. Tool turns are capped by `REM_MAX_TURNS`, and each pass writes an audit row to `data/rem_runs.json`.

REM is opt-in with `REM_ENABLED=false` by default. Configure `REM_INTERVAL_SECONDS`, `REM_EVENT_BUFFER_MAX`, `REM_RUN_HISTORY`, and `OLLAMA_REM_MODEL` in `.env`. Admins can use `,rem*` commands or the dashboard REM card; public read endpoints expose status and run history, while mutations require Basic auth.

## Dashboard / API

The API server (`api/api_server.py`) serves a dashboard and admin interface.

- **GET dashboard endpoints** are public so the public dashboard can load data.
- **Admin context endpoints** and all **POST/PUT/DELETE endpoints** require HTTP Basic auth with `MAXWELL_ADMIN_USER` / `MAXWELL_ADMIN_PASSWORD`.
- **`POST /api/login`** is exempt from middleware; credentials are validated by the handler.
- The admin HTML can be served publicly; protected actions still require API credentials inside the UI.

Static files (`web/index.html`, `web/admin/index.html`) should be copied to a web root. Reverse proxy `/api/*` and `/data/*` to `MAXWELL_API_HOST:MAXWELL_API_PORT`. See `examples/Caddyfile.example`.

## Security

- Never commit `.env`, `data/`, logs, PM2 dumps, or generated sites.
- Set real values for `MAXWELL_ADMIN_USER` and `MAXWELL_ADMIN_PASSWORD`. The API does not persist or bootstrap credentials.
- Generated bot sites serve arbitrary HTML. Host them on a separate origin from admin pages to prevent credential theft via XSS.
- The shell tool runs commands directly with `bash -lc` from the bot process environment. Only enable tools where that level of host access is intended.

## License

MIT. See `LICENSE`.

## Why am I doing this?

Just for fun idk you will see ALOT of ai slop and very specific stuff just made for my code and model so some things like audio recognition and video is for gemini and my website stuff and ect will not work for you sooo uhh yeah (your problem not mine if you have things that will help everyone like universal model selector for like adding models that have video support or dont ect please do a pull request thanks!)
