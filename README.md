# Maxwell

Maxwell is a Discord self-bot backed by any OpenAI-compatible API. It reads text, images, audio, video, file attachments, and Discord embeds, then responds using an LLM with tool-calling support. It includes a web dashboard, admin API, and temporary site generation.

**This is a self-bot** (`discord.py-self`, `self_bot=True`). Self-bots may violate Discord ToS. Use at your own risk.

## Features

- Multimodal input: images, audio, video, text files, and Discord embeds are forwarded to the model with normalized video, extracted frames, and extracted audio.
- Visual memory: recent images persist across messages per channel (configurable depth).
- Tool system: image generation (NVIDIA NIM, GPT-compatible), web search, URL fetch, YouTube transcript/frame extraction, arbitrary file sending, meme/media sending, shell execution, polls, invites, site generation, avatar/presence/nickname changes, message editing/forwarding/deletion, live tool-call progress messages, and more.
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
# edit .env — DISCORD_TOKEN is the only required value
```

### System packages

`pip install -r requirements.txt` covers the Python side. These system
packages are required for the features listed:

| Package | Needed for | Optional? |
|---|---|---|
| `ffmpeg` | TTS playback, video frame extraction, audio conversion | only required if `ENABLE_VIDEO_INPUT=true` or you use TTS |
| `libopus0` / `libopus-dev` | voice receive (live VC listening) | only required if `ENABLE_VC=true` |
| `libsodium` / `libsodium-dev` | PyNaCl (voice crypto) | only required if `ENABLE_VC=true` |
| `espeak-ng` | local TTS engine (default) | only required if `TTS_ENGINE=local` or `auto` without NVIDIA key |
| `node` or `deno` | yt-dlp YouTube JS challenge solver | only required if `ENABLE_YOUTUBE=true` |
| `postfix` + `dovecot-imapd` | email tools | only required if `ENABLE_EMAIL_TOOLS=true` |
| `docker` | opencode subagent (default backend) | only required if `ENABLE_SUBAGENT=true` and `OPENCODE_SUBAGENT_DOCKER=true` |

Debian/Ubuntu one-liner for everything except Postfix/Dovecot:

```bash
sudo apt install ffmpeg libopus0 libsodium-dev espeak-ng nodejs
```

For the YouTube tool, set `YOUTUBE_COOKIES_FILE=/path/to/cookies.txt` in
`.env` for videos that trigger YouTube bot checks. Never commit that file.

### Run it

```bash
# two terminals, or background each
python bot.py
python api/api_server.py
```

Or with PM2 (recommended for production):

```bash
pm2 start ecosystem.config.js
pm2 logs maxwell-bot maxwell-api
```

## Environment Variables

See `.env.example` for the full template with comments. The most
important ones:

### Required to start

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord user token (self-bot — may violate Discord ToS) |
| `MAXWELL_ADMIN_USER` / `MAXWELL_ADMIN_PASSWORD` | Dashboard / API Basic auth. Empty password = 503 on every request. |
| `MAXWELL_OWNER_IDS` | Comma-separated Discord user IDs allowed to run admin commands |

### LLM provider

| Variable | Description |
|---|---|
| `OLLAMA_BASE_URL` | OpenAI-compatible API base URL (default: `http://localhost:11434`) |
| `OLLAMA_API_KEY` | Bearer token (falls back to `OPENAI_COMPAT_API_KEY`) |
| `OLLAMA_MODEL` | Model name (default: `gemma4:31b-cloud`) |
| `OLLAMA_REM_MODEL` | REM dreamer model (defaults to `OLLAMA_MODEL`) |
| `OLLAMA_MAX_TOKENS` | Max output tokens per completion (default: `8192`) |
| `OLLAMA_TEMPERATURE` | Temperature (default: `1.0`) |
| `OLLAMA_FALLBACK_*` | Optional secondary endpoint, rotates with primary |
| `OLLAMA_RETRY_ATTEMPTS` | Total attempts per request (default: `3`) |
| `AUTONOMY_BASE_URL` / `AUTONOMY_API_KEY` / `AUTONOMY_MODEL` | Override the autonomy engine endpoint; blank = use main |

### Feature kill switches

All default to `true` (matches legacy behaviour). Set any of these to
`false` to skip registering the tool, importing the heavy dep, or
auto-starting the background loop. Restart the bot to apply.

| Variable | Disables |
|---|---|
| `ENABLE_IMAGE_INPUT` | Forwarding images to the LLM (also controlled via `process_images` in dashboard) |
| `ENABLE_VIDEO_INPUT` | ffmpeg video frame extraction for `video/*` attachments |
| `ENABLE_AUDIO_INPUT` | Forwarding audio to "omni" audio-capable models (default `false`) |
| `ENABLE_IMAGE_GEN` | `image_generator` + `hd_image` tools (NVIDIA NIM key required otherwise) |
| `ENABLE_TTS` | The `tts` tool |
| `ENABLE_TTS_VC` | VC TTS playback paths |
| `ENABLE_EMAIL_TOOLS` | `email_send` / `email_read_inbox` / `email_get_message` / `email_search` |
| `ENABLE_VC` | `voice_recv` import + `,vc` commands (needs `discord-ext-voice-recv`) |
| `ENABLE_YOUTUBE` | `youtube` tool (needs `yt-dlp`) |
| `ENABLE_WEB_SEARCH` | `web_search` tool (needs `ddgs`) |
| `ENABLE_FETCH_URL` | `fetch_url` tool |
| `ENABLE_SUBAGENT` | `sub_agent` tool (needs `opencode` binary) |
| `ENABLE_CREATE_SITE` | `create_site` / `list_sites` tools |
| `ENABLE_AVATAR` | `change_avatar` tool |
| `ENABLE_SHELL` | `shell` tool (host access — only enable if you trust the model) |
| `ENABLE_TELEGRAM` | Auto-start Telegram polling/webhook when `TELEGRAM_TOKEN` is set |
| `ENABLE_AUTONOMY` | Autonomy engine (also controlled via dashboard) |
| `ENABLE_REM` | Background REM dreaming pass |

### TTS engine (only used if `ENABLE_TTS=true`)

| Variable | Description |
|---|---|
| `TTS_ENGINE` | `local` (espeak, no key) / `riva` (NVIDIA, paid) / `gtts` / `auto` |
| `TTS_RIVA_*` | Riva TTS function ID, voice, language |
| `NVIDIA_API_KEY` | Required for `riva`; also required for `hd_image` |

### Image generation

| Variable | Description |
|---|---|
| `NVIDIA_API_KEY` | NVIDIA NIM key for `image_generator` (Flux) and `hd_image` |
| `NVIDIA_IMAGE_URL` | NVIDIA NIM endpoint |
| `GPT_IMAGE_URL` / `GPT_IMAGE_API_KEY` | Optional GPT-Image-2 compatible endpoint for `hd_image` |

### Admin API / dashboard

| Variable | Description |
|---|---|
| `MAXWELL_API_HOST` / `MAXWELL_API_PORT` | API bind address (default: `127.0.0.1:8765`) |
| `MAXWELL_PUBLIC_BASE_URL` | Public URL where generated sites are served |
| `MAXWELL_CORS_ORIGIN` | Allowed CORS origin |
| `MAXWELL_SITE_DIR` | Where generated sites are written (default: `public/bot`) |
| `MAXWELL_TRUST_PROXY` | Trust `X-Forwarded-For` from reverse proxy (default `false`) |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | Discord OAuth on dashboard (optional, both blank = Basic only) |

### Email (only used if `ENABLE_EMAIL_TOOLS=true`)

| Variable | Description |
|---|---|
| `MAXWELL_SMTP_HOST` / `MAXWELL_SMTP_PORT` | Postfix for outbound (default `127.0.0.1:25`) |
| `MAXWELL_IMAP_HOST` / `MAXWELL_IMAP_PORT` | Dovecot for inbound (default `127.0.0.1:993`) |
| `MAXWELL_EMAIL_USER` / `MAXWELL_EMAIL_PASSWORD` | SASL credentials |
| `MAXWELL_EMAIL_FROM` / `MAXWELL_EMAIL_FROM_NAME` | `From:` header |

### OpenCode sub-agent (only used if `ENABLE_SUBAGENT=true`)

| Variable | Description |
|---|---|
| `OPENCODE_BIN` | Path to `opencode` binary |
| `OPENCODE_SUBAGENT_BASE_DIR` | Workdir for sub-agent tasks (default `subagents/`, gitignored) |
| `OPENCODE_SUBAGENT_MODEL` | Default model (default `ollama-cloud/minimax-m3`) |
| `OPENCODE_SUBAGENT_TIMEOUT_MINUTES` | Per-task timeout (default `30`) |
| `OPENCODE_SUBAGENT_DOCKER` | Run in Docker (default `true`) |
| `OPENCODE_SUBAGENT_MEMORY` / `OPENCODE_SUBAGENT_CPUS` | Container resource limits |
| `OPENCODE_SUBAGENT_NETWORK` | `bridge` (default) or `none` |

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
| `,autonomy` | Yes | Show autonomy status + current channel/server blacklists |
| `,autonomy tick` | Yes | Trigger one autonomy check immediately |
| `,autonomy on` / `,autonomy off` | Yes | Enable or disable autonomy |
| `,autonomy log` | Yes | Show recent autonomy actions |
| `,autonomy interval <seconds>` | Yes | Set autonomy check interval |
| `,autonomy blacklist channel|server <id>` | Yes | Add to autonomy blacklist (channels or servers/guilds) |
| `,autonomy unblacklist channel|server <id>` | Yes | Remove from autonomy blacklist |
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
| `,progress on` / `,progress off` / `,progress status` | Yes | Toggle live "thinking: …" progress messages during tool calls, per server (off by default; DMs never get them) |
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

Autonomy respects two dedicated blacklists (in addition to the general `blocked_channels`/`allowed_channels`):

- `autonomy_blocked_channels`: list of channel IDs autonomy will never post to or run tools against.
- `autonomy_blocked_servers`: list of guild/server IDs autonomy will ignore entirely.

These are independent of normal bot replies, so you can keep the bot responsive on mention while preventing autonomous actions in busy or low-value servers/channels. Manage via dashboard **Controls** tab (new Autonomy Blacklists card), raw `bot_control.json`, or chat commands:

`,autonomy` — show status + current blacklists  
`,autonomy blacklist channel 123456789012345678`  
`,autonomy blacklist server 123456789012345678`  
`,autonomy unblacklist channel ...` (or server)

## Development & Releases

This is a **rolling release** project.

- `main` is always the current release.
- `git push origin main` + `pm2 restart maxwell-bot maxwell-api` is the deployment.
- No semantic version numbers, no release tags, no version branches.
- Features (like the hourly Intel knowledge roller) land continuously.

If you're running via PM2 (recommended), a push to main followed by a restart gives you the latest rolling update immediately.

> The Intel engine was removed in commit `d455e4b`. The `,intel` commands
> and the `intel_enabled` / `intel_interval_seconds` keys are stripped
> from `bot_control.json` at load. If you want a self-updating news
> roller back, see the `context_cleanup` background loop and the
> `autonomy` engine — both already write fresh facts into long-term
> memory on their own cadences.

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
