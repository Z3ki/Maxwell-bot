# Maxwell

Maxwell is a Discord self-bot backed by any OpenAI-compatible API. It reads text, images, audio, video, file attachments, and Discord embeds, then responds using an LLM with tool-calling support. It includes a web dashboard, admin API, and temporary site generation.

**This is a self-bot** (`discord.py-self`, `self_bot=True`). Self-bots may violate Discord ToS. Use at your own risk.

## Features

- Multimodal input: images, audio, video, text files, and Discord embeds are forwarded to the model with normalized video, extracted frames, and extracted audio.
- Visual memory: recent images persist across messages per channel (configurable depth).
- Tool system: image generation (Pollinations, NVIDIA NIM, GPT-compatible), web search, URL fetch, YouTube transcript/frame extraction, arbitrary file sending, meme/media sending, shell execution, polls, invites, site generation, avatar/presence/nickname changes, message editing/forwarding/deletion, and more.
- Autonomy: periodic self-directed checks where Maxwell reviews context/goals and decides whether to act without running a decider on every few messages.
- Per-server custom prompts, long-term memory, and scoped cross-context facts across DMs, servers, groups, and channels.
- Opt-in REM "dreaming" pass that periodically consolidates recent visible traffic into long-term memory.
- Web dashboard/admin API protected by HTTP Basic auth.
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

YouTube transcript and frame extraction uses `yt-dlp`, `yt-dlp-ejs`, a JavaScript runtime, and `ffmpeg`. The Python packages are included in `requirements.txt`; install `node` or `deno`, and install `ffmpeg` with your system package manager if they are not already available. Maxwell explicitly uses Node for yt-dlp's YouTube JS challenge handling when `node` is on `PATH`. For videos that trigger YouTube bot checks, export Netscape-format cookies to `data/youtube_cookies.txt` or set `YOUTUBE_COOKIES_FILE=/path/to/cookies.txt`. Never commit that file.

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
| `,autonomy` | Yes | Show autonomy status |
| `,autonomy tick` | Yes | Trigger one autonomy check immediately |
| `,autonomy on` / `,autonomy off` | Yes | Enable or disable autonomy |
| `,autonomy log` | Yes | Show recent autonomy actions |
| `,autonomy interval <seconds>` | Yes | Set autonomy check interval |
| `,intel` | Yes | Show status of the background tech/AI news & model intel gatherer |
| `,intel now` | Yes | Force an immediate news/intel gathering pass into long-term memory |
| `,intel on` / `,intel off` | Yes | Enable/disable the hourly intel gatherer |
| `,intel interval <seconds>` | Yes | Set intel gather interval (default 3600) |
| `,intel log` | Yes | Show recent intel run audits |
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

## Web and YouTube Tools

When tools are enabled, Maxwell can use `web_search` for recent/searchable info, `fetch_url` to read a specific web page, and `youtube` for YouTube videos. The YouTube tool returns title/channel/duration, transcript or auto-captions when available, using YouTube timedtext first and `yt-dlp` as fallback. Cookie-backed caption fetching uses `yt-dlp --ignore-no-formats-error --write-subs --write-auto-subs`, which can fetch captions even when playable formats are blocked. Requested timestamp frames use yt-dlp's `web_embedded` YouTube client to avoid raw Googlevideo 403s, then attach back to the model for visual inspection before Maxwell answers. Timestamps can be written like `0:10` or `1:23,2:45`.

## Memory and REM

Maxwell keeps its existing memory surfaces: `memory.json` for per-channel short-term chat history, `long_term_memory.txt` / long-term memory APIs for durable facts, and scoped shared context for cross-channel facts. REM adds a separate visible-only ring at `data/rem_events.json` and, when enabled, periodically reviews events since the previous run.

The REM pass is not a live chat response and never posts to Discord. Current code sends a bounded short-term slice plus a long-term memory snapshot to the configured OpenAI-compatible provider and stores an audit row in `data/rem_runs.json`. It does **not** currently run memory-edit tools despite the name; treat it as review/audit unless that loop gets rebuilt.

REM is opt-in with `REM_ENABLED=false` by default. Configure `REM_INTERVAL_SECONDS`, `REM_EVENT_BUFFER_MAX`, `REM_RUN_HISTORY`, and `OLLAMA_REM_MODEL` in `.env`. Admins can use `,rem*` commands or the dashboard REM card.

## Autonomy

Autonomy is separate from the removed `,auto` auto-reply mode. It wakes on `autonomy_interval_seconds`, gathers recent conversations, DMs, goals, memory, and available channels, then asks the LLM for a JSON action plan. Supported actions are channel posts, DMs, tool calls, memory updates, goal creation, or doing nothing.

Channel post cooldowns were removed. The engine can post to the same channel on consecutive ticks if the planner decides to. Keep the interval sane if you do not want spam; the code is no longer pretending a hardcoded 30-minute cooldown is wisdom.

Autonomy (and normal chat) now benefits from the **Intel engine** (`intel.py`): a background sub-process that wakes ~hourly, uses web_search + fetch to pull the latest AI model releases, LLM news, benchmarks, etc., then uses the *exact same model/provider* configured for autonomy/REM to curate facts and writes them into long_term_memory. This is how the main bot stays aware of "the new AI model that just dropped" instead of saying it doesn't know.

`,intel now` forces a pass. It is enabled by default with a 1-hour interval.

It **receives** directly from news outlet feeds (RSS) instead of searching: defaults are OpenAI (`openai.com/news/rss.xml`), Hugging Face blog, MarkTechPost, TLDR AI, arXiv cs.AI/cs.LG. Customize via `intel_feed_urls` in control or `data/intel_control.json` (`{"feed_urls": [...]}`). Run `,intel feeds` to list active sources.

## Dashboard / API

The API server (`api/api_server.py`) serves a dashboard and admin interface.

- All API/data requests require HTTP Basic auth with `MAXWELL_ADMIN_USER` / `MAXWELL_ADMIN_PASSWORD`, except `OPTIONS` preflight and `POST /api/login`.
- `POST /api/login` is exempt from middleware; credentials are validated by the handler and rate-limited.
- The admin HTML can be served publicly, but it will not load data or mutate anything until credentials are supplied.

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
