# Maxwell

Maxwell is a Discord self-bot backed by any OpenAI-compatible API. It reads text, images, audio, video, file attachments, and Discord embeds, then responds using an LLM with tool-calling support. It includes a web dashboard, admin API, and temporary site generation.

**This is a self-bot** (`discord.py-self`, `self_bot=True`). Self-bots may violate Discord ToS. Use at your own risk.

## Quick start

You need exactly two things: **a Discord token** and **a model** (any
OpenAI-compatible endpoint). Everything else is optional and turns itself on
only if what it needs is already installed.

### Prerequisites (Debian/Ubuntu)
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
```

```bash
git clone https://github.com/Z3ki/Maxwell-bot.git maxwell && cd maxwell
./setup.sh          # venv + core deps + asks for the two required values
. .venv/bin/activate
python3 bot.py
```

Prefer to do it by hand?

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in DISCORD_TOKEN, OLLAMA_BASE_URL, OLLAMA_MODEL
python3 doctor.py         # what's on, what's off, and why
python3 bot.py
```

`python3 doctor.py --probe` also calls your model and embedding endpoints, so
you find out the URL or key is wrong before the bot does.

One thing is worth knowing up front: the `shell` and `sub_agent` tools run
inside a Docker container, so they need a working Docker daemon your user can
reach. Everything else works without it. `doctor.py` reports the daemon
alongside the rest, and `ENABLE_SHELL=false` turns both tools off if you would
rather not install Docker at all.

### The minimum .env

```ini
DISCORD_TOKEN=your-discord-user-token
OLLAMA_BASE_URL=http://localhost:11434   # or https://openrouter.ai/api/v1, etc.
OLLAMA_MODEL=qwen3:8b                    # whatever your endpoint serves
```

A bare host URL gets `/v1` appended automatically; a URL that already has a
path is used as-is. Add `OLLAMA_API_KEY` if your endpoint needs a bearer token,
and `MAXWELL_OWNER_IDS` (your Discord user ID) if you want admin commands to
work.

### Optional extras

Nothing below is needed to run the bot. Install a line only if you want that
feature — each one is detected at startup, and a missing dependency turns that
one feature off instead of breaking anything.

```bash
pip install -r requirements-optional.txt   # all of it
pip install ddgs                           # or just web search
```

| You want | Install | Feature |
|---|---|---|
| Web search | `pip install ddgs` | `web_search` tool |
| YouTube | `pip install yt-dlp yt-dlp-ejs` + `node` | `youtube` tool |
| Video attachments | `ffmpeg` | frame extraction for `video/*` |
| Voice channels | `pip install PyNaCl davey discord-ext-voice-recv` + `libopus0 libsodium-dev` | live VC listening, `,vc` commands |
| Text to speech | `espeak-ng` (free), or `pip install gTTS`, or a Fish/NVIDIA key | `tts` tool, VC speech |
| Semantic memory | `ollama pull qwen3-embedding:0.6b`, or any embeddings endpoint | RAG vector recall |
| Email tools | Postfix + Dovecot, then set `MAXWELL_EMAIL_PASSWORD` | `email_*` tools |
| Posting to X | two cookies from a logged-in x.com tab (`X_AUTH_TOKEN`, `X_CT0`) | `x_post`; reading X needs nothing |

Debian/Ubuntu, everything except mail:

```bash
sudo apt install ffmpeg libopus0 libsodium-dev espeak-ng nodejs
```

### Optional features are tri-state

Every `ENABLE_*` switch takes `true`, `false`, or `auto` — and `auto` is the
default, including when you leave it out of `.env` entirely:

- `auto` — on only if the dependency is actually present on this machine.
- `true` — force on. You promise the dependency is there.
- `false` — force off. The tool is never registered and the dependency never imported.

So you never have to fill in a wall of flags to install Maxwell; you set one
only to overrule a detection result. `python3 doctor.py` prints the resolved
state of every switch with the reason, and the bot logs the same summary at
startup.

Two features are opt-in rather than auto-detected, because they spend tokens
on a timer with nobody watching: `ENABLE_REM` and `ENABLE_AUTONOMY`.

### Run it

```bash
python3 bot.py                 # the bot
python3 api/api_server.py      # dashboard + admin API (optional)
```

Or under PM2 (what the author runs in production):

```bash
pm2 start ecosystem.config.js
pm2 logs maxwell-bot maxwell-api
```

The PM2 config uses `.venv/bin/python3` when that exists, and only manages an
`ollama` process if the `ollama` binary is on your PATH (`MAXWELL_PM2_OLLAMA=true|false`
to overrule).

For the YouTube tool, set `YOUTUBE_COOKIES_FILE=/path/to/cookies.txt` in `.env`
for videos that trigger YouTube bot checks. Never commit that file.

## Features

- Multimodal input: images, audio, video, text files, and Discord embeds are forwarded to the model with normalized video, extracted frames, and extracted audio.
- Visual memory: recent images persist across messages per channel (configurable depth).
- Tool system: image generation (Pollinations, NVIDIA NIM, GPT-compatible), web search, URL fetch, YouTube transcript/frame extraction, arbitrary file sending, meme/media sending, shell execution, a native coding sub-agent, polls, invites, server join/leave (`join_server`, `leave_server`), self-service role/channel setup through a server's onboarding prompts (`server_setup`, also run automatically on join), site generation, avatar/presence/nickname changes, message editing/forwarding/deletion, live tool-call progress messages, real chess (`chess_start`/`chess_move`/`chess_state`/`chess_resign`), API usage reporting (`usage`), and more.
- Full-message context: every message in context carries its timestamp plus structured annotations for polls, app-command invocations, system/welcome events, embeds (title/description/fields/images), direct media URLs, and attachment names — including messages that never pinged Maxwell (they still reach context via memory/history).
- CAPTCHA handling: Discord's hCaptcha challenges (invite accepts, DM gates, phone checks) are auto-solved via a configured solver service (`CAPTCHA_SOLVER_SERVICE` — capsolver/2captcha), or handled human-in-the-loop: the bot hosts a one-shot solve page (`/captcha/<id>`, proxied at `MAXWELL_PUBLIC_BASE_URL`), DMs the link to the owner (fallback `CAPTCHA_FALLBACK_USER_ID`), waits for a browser solve, and retries the original request with the solved token. Auto-onboarding completes role-selection prompts when joining a server that uses `GUILD_ONBOARDING`.
- X (Twitter): `x_read` pulls the home timeline, any public account, a search, mentions, or one post; `x_post` posts, replies, quotes, likes, reposts and deletes. Reading needs no account at all; posting uses the session cookies of a logged-in browser. No paid API anywhere. See [X (Twitter)](#x-twitter).
- Autonomy: periodic self-directed checks where Maxwell reviews context/goals and decides whether to act without running a decider on every few messages.
- Per-server custom prompts, RAG vector memory, and scoped cross-context facts across DMs, servers, groups, and channels.
- RAG vector memory: messages, long-term facts, and shared context entries are embedded through any OpenAI-compatible or Ollama embeddings endpoint and stored in a SQLite vector database. Semantic search retrieves the most relevant memories for each conversation — global across all channels and servers. With no embedder reachable the bot logs one line and falls back to recent-history context.
- Opt-in REM "dreaming" pass that periodically consolidates recent visible traffic into long-term memory.
- Web dashboard/admin API protected by HTTP Basic auth.
- Site building: `create_site` publishes a whole directory (index plus any CSS/JS/subpages/data files) byte-for-byte under a configurable public URL, `edit_site` patches a published site in place, `delete_site` takes it down. Pass `backend=true` and the page gets a real server side — named values and append-only lists at `/api/site/<slug>/`, same origin, no key — so a guestbook, counter, poll, or saved state is one `fetch()` away.
- Lean chat turns: ordinary conversation ships a small conversational tool set instead of the whole catalog (~83% fewer tool tokens per message). Anything that asks for an action gets everything, and `more_tools` reopens the catalog mid-turn. Turn it off with `lean_chat_tools` in the dashboard.

## Sub-agent

`sub_agent` hands a self-contained coding task to another instance of Maxwell:
same provider, its own scratch directory under `SUBAGENT_BASE_DIR`, and a small
toolset (run a command, read/write/list files, finish). It writes the code, runs
it, fixes what breaks, and reports back — then that report comes back into the
conversation as the tool result.

There is no external coding-agent binary: the sub-agent is Maxwell, so it
follows `ENABLE_SHELL` unless you set `ENABLE_SUBAGENT` explicitly, and it
refuses any path outside its workdir. Budgets: `SUBAGENT_MAX_STEPS` (100),
`SUBAGENT_TIMEOUT_SECONDS` (900), `SUBAGENT_COMMAND_TIMEOUT_SECONDS` (120).

`run_command` executes inside a throwaway Docker container, one per run, off
the same `docker/Dockerfile` image the shell tool uses. Only the run's own
scratch workspace is mounted (at `/home/maxwell/work`); the bot's source and
`.env` are not visible to it. The container is hardened the same way as the
shell sandbox — bridge network, `--cap-drop ALL` plus a small add-back set,
`no-new-privileges`, 4 GB / 2 CPU / 1024 pids — and is torn down when the run
ends. State does persist *within* a run, so an installed package or a built
binary survives to the next step.

`SUBAGENT_SANDBOX=host` opts back out and runs `bash -lc` in the bot's own
environment instead. That is a real choice on a machine you already treat as
disposable, and it has to be made explicitly: with the default setting and no
Docker daemon, the tool returns an error rather than quietly running
unsandboxed.

### Watching a run

A sub-agent run is minutes of work, and it used to be minutes of silence — the
channel saw nothing until the final report. Runs now publish events as they
happen (`agent_events.py`): the channel progress message shows the current step
and what it is doing (`step 3/100 · running: pytest -q`), and the dashboard's
Autonomy tab lists live and recent runs with their step counts, files written,
and final reports. The stream is in-process and non-durable — the durable
record of a run is its report in the channel.

## Project Structure

```
bot.py              Main bot entry point
bot_tools.py        Tool implementations
providers.py        OpenAI-compatible provider wrapper
config.py           Environment-backed configuration (incl. feature detection)
rag_memory.py       RAG vector memory (SQLite + numpy + embeddings API)
context_budget.py   Splits the prompt's memory chars across the memory tiers
agent_events.py     Live event bus for sub-agent runs (progress + dashboard)
x_client.py         X (Twitter): read backends, posting, mention poller
site_backend.py     Per-site datastore behind /api/site/<slug>/ (generated sites)
site_server.py      Per-site backend containers behind /bot/<slug>/api/
doctor.py           Install check: what works, what doesn't, why
setup.sh            One-command installer
api/api_server.py   Dashboard and admin API server
web/                Static dashboard files (index.html, admin/)
examples/           Caddyfile and PM2 config examples
docker/             Shell sandbox Dockerfile (docker/site-runtime/ for site backends)
shelldocker/        Bind-mounted working directory for the shell container
control_defaults.py Canonical DEFAULT_CONTROL — bot and API both import it
autonomy_social.py  Conversational turn-taking (the autonomy floor)
watch_policy.py     Conversation-watch + extraction scoring
ecosystem.config.js PM2 process config
```

## Environment Variables

See `.env.example` for the full template with comments — it is ordered so the
required values are the first thing in the file. The ones that matter:

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord user token (self-bot — may violate Discord ToS) |
| `OLLAMA_BASE_URL` | Any OpenAI-compatible API base URL. A bare host gets `/v1` appended. |
| `OLLAMA_MODEL` | Model name your endpoint serves. No default — an unset value fails at startup with a clear message instead of a 404 later. |

### Strongly recommended

| Variable | Description |
|---|---|
| `OLLAMA_API_KEY` | Bearer token, if your endpoint needs one (falls back to `OPENAI_COMPAT_API_KEY`) |
| `MAXWELL_OWNER_IDS` | Comma-separated Discord user IDs allowed to run admin commands. Empty = every admin command is denied. |
| `MAXWELL_ADMIN_USER` / `MAXWELL_ADMIN_PASSWORD` | Dashboard / API Basic auth. Empty password = 503 on every request. |

### LLM provider

| Variable | Description |
|---|---|
| `OLLAMA_REM_MODEL` | REM dreamer model (defaults to `OLLAMA_MODEL`) |
| `OLLAMA_MAX_TOKENS` | Max output tokens per completion (default: `8192`) |
| `OLLAMA_TEMPERATURE` | Sampling temperature (default: `0.7`) |
| `OLLAMA_FALLBACK_*` | Optional secondary endpoint, rotates with primary |
| `OLLAMA_VISION_*` | Optional vision/omni model for image/video turns (blank base/key inherit primary) |
| `OLLAMA_RETRY_ATTEMPTS` | Total attempts per request (default: `3`) |
| `AUTONOMY_BASE_URL` / `AUTONOMY_API_KEY` / `AUTONOMY_MODEL` | Override the autonomy engine endpoint; blank = use main |
| `AUX_BASE_URL` / `AUX_API_KEY` / `AUX_MODEL` | Background context agents; blank = fall back to autonomy, then main |

### Optional features

`true` / `false` / `auto`, default `auto` (on only if the dependency is
present). Restart to re-detect. `python3 doctor.py` shows the resolved state.

| Variable | Controls | `auto` needs |
|---|---|---|
| `ENABLE_IMAGE_INPUT` | Forwarding images to the LLM; the hard switch, `false` wins over the dashboard's `process_images` | — |
| `ENABLE_VIDEO_INPUT` | Video frame extraction for `video/*` attachments | `ffmpeg` |
| `ENABLE_AUDIO_INPUT` | Forwarding audio to "omni" audio-capable models | opt-in (`false` by default) |
| `ENABLE_IMAGE_GEN` | `image_generator` (NVIDIA Flux) + `hd_image` (Gemini, generate **and** edit) | — |
| `ENABLE_TTS` | The `tts` tool | espeak-ng, gTTS, or a Fish/NVIDIA key |
| `ENABLE_TTS_VC` | TTS playback into voice channels | `ffmpeg` + a TTS engine |
| `ENABLE_VC` | `voice_recv` import + `,vc` commands | `discord-ext-voice-recv` + PyNaCl |
| `ENABLE_WEB_SEARCH` | `web_search` tool | the `ddgs` package |
| `ENABLE_YOUTUBE` | `youtube` tool | the `yt-dlp` binary |
| `ENABLE_FETCH_URL` | `fetch_url` tool | — |
| `ENABLE_CREATE_SITE` | `create_site` / `edit_site` / `delete_site` / `list_sites` tools | — |
| `ENABLE_AVATAR` | `change_avatar` tool | — |
| `ENABLE_EMAIL_TOOLS` | The four `email_*` tools | `MAXWELL_EMAIL_PASSWORD` set |
| `ENABLE_X` | `x_read` / `x_post` | — (public reads need no credentials) |
| `ENABLE_SHELL` | `shell` tool (host access — only enable if you trust the model) | — |
| `ENABLE_SUBAGENT` | `sub_agent` tool (writes and runs code) | follows `ENABLE_SHELL` |
| `ENABLE_RAG` | RAG vector memory; `false` makes no embedding calls at all | — |
| `ENABLE_TELEGRAM` | Auto-start Telegram polling/webhook when `TELEGRAM_TOKEN` is set | — |
| `ENABLE_AUTONOMY` | Autonomy engine; `false` never starts the loop (the dashboard's `autonomy_enabled` is the runtime toggle) | opt-in in `.env.example` |
| `ENABLE_REM` | Background REM dreaming pass (alias of `REM_ENABLED`) | opt-in (`false` by default) |

### RAG embeddings (only used if `ENABLE_RAG` is on)

| Variable | Description |
|---|---|
| `MAXWELL_EMBED_BASE_URL` | Embeddings endpoint (default `http://localhost:11434`). A bare host uses Ollama's `/api/embed`; a `/v1` base uses OpenAI's `/v1/embeddings`. Both response shapes are parsed. |
| `MAXWELL_EMBED_MODEL` | Embedding model (default `qwen3-embedding:0.6b`) |
| `MAXWELL_EMBED_API_KEY` | Bearer token for hosted embedding endpoints |
| `MAXWELL_EMBED_DIM` | Vector dimension, must match the model (default `1024`) |

### CAPTCHA handling

| Variable | Description |
|---|---|
| `CAPTCHA_SOLVER_SERVICE` | `capsolver` or `2captcha` — auto-solves Discord captcha challenges (requires `CAPTCHA_SOLVER_API_KEY`) |
| `CAPTCHA_SOLVER_API_KEY` | API key for the solver service |
| `CAPTCHA_SOLVER_TIMEOUT` | Max seconds to wait for a captcha solution (default 180) |
| `CAPTCHA_HUMAN_SOLVE` | `true` (default) — host a human-solve page + DM the link when no auto-solver is configured/fails |
| `CAPTCHA_HUMAN_PORT` | Local port for the solve-page server (default 8790; Caddy proxies `/captcha/*` to it) |
| `CAPTCHA_FALLBACK_USER_ID` | Discord user ID to DM captcha solve links when no admin is resolvable |

### TTS engine (only used if `ENABLE_TTS=true`)

| Variable | Description |
|---|---|
| `TTS_ENGINE` | `local` (espeak, no key) / `riva` (NVIDIA, paid) / `gtts` / `auto` |
| `TTS_RIVA_*` | Riva TTS function ID, voice, language |
| `ASR_RIVA_FUNCTION_ID` | NVIDIA Riva ASR (Parakeet) function ID for live VC transcription |
| `ASR_RIVA_LANGUAGE` | ASR language code (default `en-US`) |
| `NVIDIA_API_KEY` | Required for Riva TTS/ASR and for `image_generator` |

### Image generation

| Variable | Description |
|---|---|
| `NVIDIA_API_KEY` | NVIDIA NIM key for `image_generator` (Flux) |
| `NVIDIA_IMAGE_URL` | NVIDIA NIM endpoint |
| `GEMINI_IMAGE_BASE_URL` / `GEMINI_IMAGE_API_KEY` | Endpoint for `hd_image`. Blank inherits `OLLAMA_BASE_URL` / `OLLAMA_API_KEY` |
| `GEMINI_IMAGE_MODEL` | Image model for `hd_image` (default `gemini-3.1-flash-image`) |
| `GEMINI_IMAGE_MAX_INPUT_EDGE` | Longest edge an input image is downscaled to before upload (default `1024`) |
| `GEMINI_IMAGE_TIMEOUT` | Per-request timeout in seconds (default `300`) |
| `GPT_IMAGE_URL` / `GPT_IMAGE_API_KEY` | Unused. The old GPT-Image-2 host dropped its image models; kept so existing `.env` files still load |

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
| `MAXWELL_EMAIL_IGNORE_SENDERS` | Senders never filed as inbox notices — comma-separated addresses, or leading-dot domains (`.google.com`). Empty by default |

### X / Twitter (only used if `ENABLE_X` is on)

| Variable | Description |
|---|---|
| `X_AUTH_TOKEN` / `X_CT0` | The two cookies from a logged-in x.com tab. This is the whole write credential — treat it like `DISCORD_TOKEN`. Blank = read-only |
| `X_HANDLE` | The account those cookies belong to, no `@`. Needed for mentions |
| `X_BACKEND` | `auto` (default: cookies → api → rss → syndication) or a pinned subset |
| `X_API_BASE_URL` / `X_API_KEY` / `X_API_KEY_HEADER` / `X_API_PATHS` | Your own gateway, if you already run one. `X_API_PATHS` is a JSON map of path templates |
| `X_RSS_BASE_URL` / `X_RSS_PATHS` | A Nitter or RSSHub instance for credential-free reading |
| `X_SYNDICATION` | X's own embed backend (default `true`). The zero-config read source |
| `X_MAX_CHARS` | Post length limit (default `280`; premium accounts can raise it) |
| `X_TIMEOUT_SECONDS` | Per-request timeout (default `20`) |
| `X_GRAPHQL_FILE` | Where the internal query ids live (default `data/x_graphql.json`) |

### Sub-agent (only used if `ENABLE_SUBAGENT` is on)

| Variable | Description |
|---|---|
| `SUBAGENT_BASE_DIR` | Where sub-agent workdirs are created (default `data/subagents`, gitignored) |
| `SUBAGENT_MODEL` | Model for sub-agent work; **blank = the main `OLLAMA_MODEL`** (the sub-agent runs on the same model as the chat) |
| `SUBAGENT_MAX_STEPS` | Tool-call steps before it must report back (default `100`, budget not target — finish early) |
| `SUBAGENT_TIMEOUT_SECONDS` | Wall-clock budget for one task (default `900`) |
| `SUBAGENT_COMMAND_TIMEOUT_SECONDS` | Per-command timeout (default `120`) |
| `SUBAGENT_MAX_FILE_BYTES` | Largest file the sub-agent may write (default `200000`) |
| `SUBAGENT_MAX_CONCURRENT` | How many background (fire-and-forget) sub-agents run at once (default `5`) |
| `SUBAGENT_MAX_QUEUED` | Hard ceiling on background sub-agents submitted but not finished; past this new ones are refused (default `16`) |
| `SUBAGENT_HANDOFF_TIMEOUT_SECONDS` | Maximum time Maxwell gets to compose a finished-run reply before the report is posted directly (default `30`) |
| `SUBAGENT_SANDBOX` | `docker` (default) runs each sub-agent run in its own container; `host` runs commands in the bot's own environment with no isolation |

By default Maxwell **delegates heavy multi-step work to `sub_agent`** — a full
site build, a program/script, a data-crunching or file-conversion job, anything
needing several build/test rounds — instead of grinding a long inline `shell`
chain that bloats the main context and crawls. Toggle this at runtime
with the `subagent_delegate` control key (API `POST /api/control`); `true`
(default) prefers delegation, `false` restores the old inline-first behaviour.
Only matters when `ENABLE_SUBAGENT` is on and `sub_agent` is in the turn's tool set.

Sub-agent runs have two modes. `mode=background` (the default for heavy work)
returns immediately — Maxwell keeps the turn going and the run posts the result
when it finishes, so nobody stares at a silent typing indicator for minutes.
`mode=foreground` blocks for the report and hands it back in-turn. `deliver`
controls where the result lands: `channel` (default, where it was asked) or
`dm` to the person who asked (used for long or private results in a busy
channel). Background runs are capped by `SUBAGENT_MAX_CONCURRENT`, and a
channel flood can't pile them up without bound — past `SUBAGENT_MAX_QUEUED` new
ones are refused. Both run in-process and are lost on a process restart (the
durable record of what one did is its report on the channel). Multi-step
terminal work (install → build → test, a command loop) is `sub_agent`, not
`shell` — the sub-agent owns the live channel progress.

Sub-agents are two-way now: a running sub-agent can `message_main` to flag a
blocker or ask for a decision (posted to the channel), and Maxwell can reply
with `sub_agent_message(run_id, text)`, which is injected into the sub-agent's
next step so it answers and carries on.

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
| `,solo` / `,solo #channel` / `,solo off` | Yes | Lock this server to ONE channel: Maxwell answers there and is silent in every other channel, and autonomy stops starting things here. Per-server — other servers are untouched. |
| `,jailbreak on` / `,jailbreak off` | Yes | Toggle freedom-mode prompt for this server (Discord only; Telegram always on) |
| `,blacklist [user]` | Yes | Add/view/clear blacklisted users |
| `,unblacklist [user]` | Yes | Remove a user from the blacklist |
| `,context` | Yes | Show relevant scoped cross-context facts |
| `,context all` | Yes | Show recent shared context facts |
| `,context add [scope] <fact>` | Yes | Manually add a scoped context fact |
| `,context forget <id>` | Yes | Delete a shared context fact |
| `,context private <id>` | Yes | Mark a shared context fact private |
| `,context global <id>` | Yes | Promote a fact to global shared context |
| `,progress on` / `,progress off` / `,progress status` | Yes | Toggle live "thinking: …" tool progress messages, per server (off by default; the sub-agent posts no heartbeat — Maxwell checks it via `sub_agent_status`; DMs never get them) |
| `,rem` | Yes | Show REM status and last audit preview |
| `,rem now` | Yes | Trigger one REM dream pass immediately |
| `,rem on` / `,rem off` | Yes | Enable or disable REM for this process |
| `,rem audit [N]` | Yes | Show recent REM run audits |
| `,rem fix` | Yes | Restore REM prompt/interval/max-turn defaults |
| `,x` / `,x status` | Yes | What X can do here: backends, posting, hourly budget |
| `,x read [@handle]` | Yes | Home timeline, or that account's posts |
| `,x search <query>` / `,x tweet <id\|url>` | Yes | One search or one post |
| `,x post <text>` | Yes | Post to X by hand (spends the hourly budget) |
| `,vc join` | No | Join your current VC and start live listening |
| `,vc leave` | No | Stop listening and disconnect from VC |
| `,vc listen` | No | Start live VC listening while staying connected |
| `,vc unlisten` | No | Stop live VC listening while staying connected |
| `,vc status` | No | Show VC connection/listening and voice settings |
| `,vc say <text>` | No | Speak text in VC with TTS |

Live VC replies require `discord-ext-voice-recv`, `PyNaCl`, `ffmpeg`, and an audio-capable OpenAI-compatible provider.

## Sites

`create_site` writes a directory, not a single file. `body` is index.html;
`files` is anything else — `{"style.css": "...", "app.js": "...",
"about/index.html": "..."}` — and it is all served exactly as written, with no
injected wrapper, house style, or meta tags. (Set `site_inject_csp` if your
host serves generated pages without a CSP of its own.)

`edit_site` changes a live site at the same URL: `list` its files, `read` one
back, `write` a new one, `replace` an exact string inside one (the cheap way to
fix a colour or a typo without resending the page), `delete` a file, `rename`
the title, or `extend` its lifetime. `delete_site` removes the whole thing.
Sites expire after `site_ttl_hours` (24 by default, `0` disables expiry);
`permanent=true` opts one site out.

### Site backends

A site created with `backend=true` gets a datastore on the same origin, so
page JavaScript can talk to it with a plain `fetch` — no key, no CORS:

| Route | What it does |
|---|---|
| `GET /api/site/<slug>/kv` | every named value (`?key=NAME` for one) |
| `PUT /api/site/<slug>/kv` | `{"key": ..., "value": ...}` |
| `POST /api/site/<slug>/kv/bump` | `{"key": ..., "by": 1}` — atomic counter |
| `DELETE /api/site/<slug>/kv?key=NAME` | drop a value |
| `GET /api/site/<slug>/items/NAME` | list entries (`?limit=`, `?after=ID`) |
| `POST /api/site/<slug>/items/NAME` | append an entry |
| `DELETE /api/site/<slug>/items/NAME?id=ID` | remove one (`?all=1` for all) |

These routes are **public by design** — a visitor's browser is the client, so
they carry no admin credentials and are the only unauthenticated part of the
API. Everything is bounded: 64KB per value, 1000 entries per list (oldest drop
off), 1MB per site, and a per-IP token bucket on writes. Anyone with the URL
can post, so don't put secrets in a site store and expect junk in open forms.
Data lives in `data/site_data/<slug>.json` and dies with the site.

### Real backend servers

`backend=true` is a datastore: it remembers things, but it cannot run code,
keep a secret, or enforce a rule. When a site needs an actual server —
a hidden API key, real auth, computation, a database it queries — `site_server`
gives it one:

```
site_server(name="mysite", action="write",
            files={"app.py": "...flask app..."},
            env={"WEATHER_KEY": "..."})
```

That writes the source, launches a container, and the site's routes are live at
`/bot/mysite/api/...` — a route the app defines as `/notes` answers at
`/bot/mysite/api/notes`. Other actions: `start`, `stop`, `restart`, `status`,
`logs` (the app's own stdout/stderr, which is how it debugs itself), `read`,
`env`, `delete`.

The contract the app is held to:

| | |
|---|---|
| Entry | `app.py`, listening on `0.0.0.0:$PORT` |
| Installed | Python 3.12 + flask, waitress, fastapi, uvicorn, websockets, sqlalchemy, bcrypt, pyjwt, itsdangerous, requests, httpx, jinja2, pillow, stdlib |
| Anything else | `packages=["redis==5.0.1"]` builds a per-site image |
| WebSockets | Supported end to end — use **fastapi + uvicorn**, since waitress cannot do sockets. SSE and streaming responses work too. |
| Writable | `/data` only, and only `/data` survives a restart — the database goes at `/data/app.db` |
| Secrets | `env={...}`, stored outside the site directory, never served, never echoed back, read via `os.environ` |
| Outbound | Allowed — this is where a key-carrying API call belongs |
| Limits | 256MB, half a core, 128 pids, 32MB uploads, no capabilities, read-only root, unprivileged uid |

So a site can have real user accounts (bcrypt + JWT), a database it queries,
and live multiplayer over WebSockets — the browser opens
`new WebSocket(location.origin.replace("http", "ws") + "/bot/<slug>/api/ws")`
and lands on the site's own server.

How it is contained: code lives in `data/site_servers/<slug>/`, **outside the
web root**, so source and secrets are never static files. Each site gets its own
container from `maxwell-site-runtime` (`docker/site-runtime/`) with `--cap-drop
ALL`, `--read-only`, no docker socket, no host filesystem, and its port
published on `127.0.0.1` only — the sole public path is the proxy, which takes
its destination from the registry, never from the request. `--restart
unless-stopped` brings backends back after a reboot; the bot reconciles the
registry on boot. Deleting or expiring a site destroys its container, code,
database, and secrets.

It is still a container running model-written code with outbound network
access, so it is exactly as trusted as `ENABLE_SHELL` — treat it that way.

Routing needs one line in your reverse proxy, **before** the static `/bot/*`
rule (see `examples/`):

```
handle /bot/*/api/* {
        reverse_proxy 127.0.0.1:8765
}
```

## Web and YouTube Tools

When tools are enabled, Maxwell can use `web_search` for recent/searchable info, `fetch_url` to read a specific web page, and `youtube` for YouTube. Video URLs return title/channel/duration plus transcript or auto-captions when available (YouTube timedtext first, `yt-dlp` as fallback). Channel, handle, playlist, and `/videos` URLs list recent uploads instead of trying to caption the whole channel. `query` runs a YouTube search. Cookie-backed caption fetching uses `yt-dlp --ignore-no-formats-error --write-subs --write-auto-subs`. Requested timestamp frames use yt-dlp's `web_embedded` YouTube client, then attach back to the model. Timestamps can be written like `0:10` or `1:23,2:45`. Listings are cached for a few minutes so auto-invokes don't 429 YouTube.

## X (Twitter)

He could be quoted at all day and never look at the thing himself. Now he can,
and none of it costs money — there is no developer account and no paid tier
anywhere in this feature.

The two halves are deliberately separate:

**Reading is free and needs no account.** X's own embed backend (the one that
renders quoted tweets on other people's blogs) serves any public profile and
any single post, and it is on by default with nothing to configure. Point
`X_RSS_BASE_URL` at a Nitter or RSSHub instance and search works too.

**Writing needs an account**, and the free way to have one is the session of a
browser already logged in as him:

1. log into x.com in a normal browser
2. devtools → Application → Cookies → `https://x.com`
3. copy `auth_token` into `X_AUTH_TOKEN` and `ct0` into `X_CT0`, and set `X_HANDLE`

Those two cookies **are** the account — treat them exactly like
`DISCORD_TOKEN`. They also unlock the two reads no anonymous endpoint can
serve: the home timeline and mentions. Driving an account this way is against
X's ToS, which is the same bet as the Discord self-bot this whole project is.

Already run your own gateway or scraper? `X_API_BASE_URL` is tried before the
public sources, and `X_API_PATHS` remaps its routes if they differ from the
defaults.

### The two tools

```
x_read  action=home | user | search | mentions | tweet
x_post  action=post | reply | quote | delete | like | repost
```

`x_read` renders each post with its id, so replying to one is
`x_post action=reply reply_to=<id>`. A post's media URLs come back too, so
`see_image` can look at the picture. X search operators work as written —
`from:nasa`, `min_faves:500`, `-filter:replies`, `lang:en`.

### Backends and fallback

| Backend | Needs | Can read | Can write |
|---|---|---|---|
| `cookies` | `X_AUTH_TOKEN` + `X_CT0` | everything | yes |
| `api` | `X_API_BASE_URL` | everything your gateway serves | yes |
| `rss` | `X_RSS_BASE_URL` | user, search | no |
| `syndication` | nothing | user, tweet | no |

Reads walk that list and take the first backend that answers, so an expired
cookie or a dead Nitter instance is a slower read rather than a dead feature.
Writes deliberately do **not** fall through: a post landing from a different
path than you expected is a surprise, so a failed write is reported as failed.

X's internal GraphQL query ids rot every few weeks. They live in
`data/x_graphql.json` (`{"ids": {"CreateTweet": "..."}}`) and any call that
404s says exactly where to copy a fresh one from. Stale ids cost the cookie
backend, not the feature — reads fall through to syndication and only posting
actually stops. A missing feature flag heals itself: X names the flag in the
error, the client adds it and retries once.

### Guardrails

Posting is the least reversible thing in the whole tool catalog, so it has
more than a prompt holding it back:

| Control | Default | What it does |
|---|---|---|
| `x_post_enabled` | `true` | Master switch for every write. Off leaves reading intact |
| `x_posts_per_hour` | `8` | Hard rolling-hour ceiling, enforced against a persisted log so a crash loop cannot reset it. `0` = never post |
| `x_autonomy_post` | `false` | Whether the unattended autonomy tick may post at all — separate from answering someone who asked |
| `x_cache_seconds` | `60` | Identical reads reuse the last answer instead of spending rate-limit budget |
| `x_mention_poll_seconds` | `300` | How often mentions become inbox notices |

`x_post` is also taint-gated like `shell` and `email_send`: a turn that read a
web page, a search result, or X itself needs an out-of-band `,confirm` before
it can publish, because "post this" is exactly what an injected page would
say. `DISABLE_TAINT_GATE=true` turns that off install-wide.

### Mentions

Someone @-ing him on X is someone waiting on him, so mentions file as inbox
notices next to friend requests and mail — one notice per post ever, his own
posts never filed, a dismissed mention staying dismissed. It needs a session
(mentions are not public), and the poller stays quiet and says so once when
there is nothing to read them with.

## Chess

Maxwell plays real chess against whoever starts a game. One game per channel,
and only the player who started it may move — that player is the one Maxwell
focuses on there. The chess tools own the board state, render the position as
a PNG (white at the bottom, last move and check highlighted), post that image
to the channel so the player sees it, and return the board as text + FEN +
legal moves plus the image as base64 so Maxwell sees it on the next turn.

| Tool | What it does |
|---|---|
| `chess_start` | Start a game against the invoking player. `bot_side=white\|black\|auto` (default white); `depth` 1-4 (default 3). If Maxwell is white it opens automatically. |
| `chess_move` | Play a move in SAN (`e4`, `Nf3`, `O-O`) or UCI (`e2e4`). Player's move is relayed; Maxwell's own move is a legal choice or, if omitted on its turn, picked by a small alpha-beta engine. `respond=true` (default) makes Maxwell reply right after a player move. |
| `chess_state` | Re-sync: current board, FEN, legal moves, whose move — no board change, no post. |
| `chess_resign` | End the game (`side=maxwell` or `side=player`). |

Maxwell's engine is a depth-limited negamax with material + piece-square
evaluation and a small opening book, so it plays a sane opening instead of
1.a3. Games persist in `data/chess_games.json` (gitignored) and survive a
restart. Deps: `python-chess` + `pillow` (already in `requirements.txt`).

## Usage

The `usage` tool queries the provider quota endpoint (`z3ki.dev/v2/usage`)
with the API key already in the environment (`OLLAMA_API_KEY`, falling back to
`OPENAI_COMPAT_API_KEY`), returning remaining percentage and reset times.
Override the URL with `MAXWELL_USAGE_URL`.

## Memory and RAG

Maxwell uses a **RAG (Retrieval-Augmented Generation) vector memory system** backed by SQLite and numpy. All channel messages, long-term facts, and shared context entries are stored as vectors in `data/maxwell_rag.db`, embedded through whatever endpoint `MAXWELL_EMBED_BASE_URL` points at — a local Ollama running `qwen3-embedding:0.6b` by default, or any OpenAI-compatible `/v1/embeddings` service.

RAG is optional. With no embedder reachable, the bot logs one line, stops calling the endpoint for a cooldown, and keeps working on recent-history context; `ENABLE_RAG=false` skips embedding entirely.

**How it works:**
- Every message stored in the bot is embedded (1024-dim float32 vector) and saved to the SQLite vector store.
- When the bot is pinged, the user's message is embedded and cosine-similarity search retrieves the most relevant memories across **all channels, servers, LTM, and shared context** — not just the current channel's recent history.
- Per-query latency: ~150ms (query embedding) + ~1ms (cosine search) = negligible compared to LLM generation time.
- New messages are embedded in the background (non-blocking).
- On startup, any vectors without embeddings are batch-embedded in the background.
- The old `memory.py` (flat JSON) and `context_cleanup.py` (LLM janitor) have been removed. The RAG system handles dedup, pruning, and retrieval automatically.

**Embedding model setup** (the free local default):
```bash
ollama pull qwen3-embedding:0.6b
```

Or point it somewhere else, e.g. OpenAI:
```ini
MAXWELL_EMBED_BASE_URL=https://api.openai.com/v1
MAXWELL_EMBED_MODEL=text-embedding-3-small
MAXWELL_EMBED_API_KEY=sk-...
MAXWELL_EMBED_DIM=1536
```

Changing the model or dimension invalidates existing vectors: delete
`data/maxwell_rag.db` (or accept that old rows stop matching) when you switch.

The SQLite database lives at `data/maxwell_rag.db` (gitignored). Channel memory, LTM, shared context, and per-user entity facts are all in one `vectors` table distinguished by `kind` (`message`, `ltm`, `shared_context`, `entity`), alongside a `user_entities` table holding identity.

### Global user memory

A Discord user id is already global — the same person in two servers and a DM
is one id — but nothing used that, so the bot could learn your name in one
server and meet you as a stranger in the next. It now keeps a row per user id,
independent of guild: the names they have gone by, where they have been seen,
and durable facts about them. All of it is read back regardless of which server
or DM the current message arrived in, and it renders as its own prompt block
("About this person"). Facts arrive from the context extractor's `user:`- and
`dm:`-scoped output; admin-only ones are deliberately not mirrored, since
material that should not follow someone between servers is exactly what this
tier would carry. The dashboard's Memory tab lists the roster under **People**,
and `GET /api/rag/entities` serves it.

Controls: `entity_memory_enabled`, `entity_memory_max_items`,
`entity_memory_from_extract`.

### Per-tier context budget

The prompt is assembled from several memory tiers — the channel transcript,
recalled long-term facts, the entity profile, cross-context facts, and cached
web results. Each used to be capped by an *item count*, which is a bad proxy
for size: fifty one-line facts and fifty paragraph-long ones differ by two
orders of magnitude. The combined size swung wildly, and the transcript — which
is assembled last and sits in the middle of the message list where the
whole-prompt trim cannot reach it — absorbed every overshoot.

`context_budget.py` now divides the available characters across the tiers by
weight before any of them render, and each is trimmed to fit its share. A tier
that comes in under budget hands the remainder to the tiers after it, and
whatever the lookup tiers leave over goes to the transcript — so the tier that
carries the actual conversation is the one that benefits from a quiet turn,
rather than the one that pays for a noisy one. Weights are
`context_tier_recent_weight` and friends (default 70/12/8/7/3); a weight of 0
switches a tier off and redistributes its share.

REM adds a separate visible-only ring at `data/rem_events.json` and, when enabled, periodically reviews events since the previous run.

The REM pass is not a live chat response and never posts to Discord. Current code sends a bounded short-term slice plus a long-term memory snapshot to the configured OpenAI-compatible provider and stores an audit row in `data/rem_runs.json`. It does **not** currently run memory-edit tools despite the name; treat it as review/audit unless that loop gets rebuilt.

REM is opt-in: it is off unless you set `ENABLE_REM=true` (or `REM_ENABLED=true`) in `.env`. Configure `REM_INTERVAL_SECONDS`, `REM_EVENT_BUFFER_MAX`, `REM_RUN_HISTORY`, and `OLLAMA_REM_MODEL` in `.env`. Admins can use `,rem*` commands or the dashboard REM card.

### Inbox notices

An inbox item is either a *notice* (mail, a group-DM add — something to be
told) or a *request* someone is waiting on (a friend request). Requests keep
showing in the prompt until they are accepted or declined; notices drop out of
it once he has actually said them out loud, which happens automatically after
the reply that mentioned them is delivered. He can also do it by hand with
`inbox_action action=read`, which works on any item whatever actions it
declares — for something he has decided not to mention at all.

Before that, `read` only reordered an item, so a notice had no way out of the
prompt short of an explicit `dismiss` — the same email was announced on every
turn, reworded each time. Leaving the prompt is not leaving the inbox:
`inbox_list` still shows read notices, and `dismiss` is still what clears one
for good.

**Mail from his own address is never filed.** A self-copy — a server-side
`always_bcc`, a self-BCC, a list that reflects the post back — arrives in INBOX
like anything else, and was announced as though a stranger had written in, so
he narrated his own outbox. Both the mailbox login and `MAXWELL_EMAIL_FROM`
count as his.

For machine mail there is `MAXWELL_EMAIL_IGNORE_SENDERS`: a comma-separated
list of addresses, or leading-dot domains (`.google.com`) covering a domain and
its subdomains. It is **empty by default**, deliberately — which machine mail
matters is your call. A DMARC aggregate report is pure telemetry, but a
`MAILER-DAEMON` bounce means something he sent did not arrive, and nothing can
tell those apart by shape. Ignoring a sender only skips the inbox row; the mail
stays on the server and the `email_*` tools still read it.

### Repetition

Two different problems that read as one complaint ("it keeps saying jajajaja").

Inside a single reply, a laugh run, a doubled word, a sentence said twice or a
phrase repeated is collapsed before the message is sent. `response_guard.py`
has done this since it was written — it just was not wired to anything, so
every run reached the channel intact. Fenced code is never touched, and a reply
that has fallen into a full echo loop is truncated at the first repeat rather
than posted whole. Control: `scrub_repetitions`.

Across replies, the same phrase opening six messages running is a pattern no
single message is wrong for, so nothing downstream can catch it — and the model
does not notice it in its own transcript, because it reads its last reply as
evidence of what it sounds like and does it again. When several of his recent
messages open the same way (laugh runs of different lengths count as one habit)
the prompt says so, naming the phrase: general advice like "vary your language"
changes nothing, "you have opened 4 of your last 8 messages with jajaja" does.
Control: `self_repetition_note_enabled`.

## Autonomy

Autonomy is separate from the removed `,auto` auto-reply mode. It wakes on `autonomy_interval_seconds`, gathers recent conversations, DMs, goals, memory, and available channels, then asks the LLM for a JSON action plan. Supported actions are channel posts, DMs, tool calls, memory updates, goal creation, or doing nothing.

### The four stages

One tick is **observe → plan → policy gate → execute**.

| Stage | Method | What it does |
| --- | --- | --- |
| Observe | `observe()` | Reads the world into the planner's context, bounded so one hung fetch cannot freeze the loop. |
| Plan | `plan(context)` | Asks the model for a validated action list. |
| Policy gate | `policy_gate(actions)` | Rules on each action — tool allowlist, one-post-per-room, turn-taking — without side effects. |
| Execute | `run_allowed(verdicts)` | Runs what survived. |

Those stages always existed, but only two of them had names: the gate was a
block of `continue` statements inside `execute`, so a denied action and a
failed action produced the same shape of result and nothing could report "the
plan was fine, policy stopped it". Denials now carry a code (`floor`,
`duplicate_post`, `tool_blocked`) and are counted separately from errors in the
tick summary. `execute(actions)` still gates-then-runs in one call for the many
callers that want plan-in, results-out.

The gate deliberately runs at execution time rather than at plan time: the plan
is seconds stale by the time it lands — someone starts typing, the live bot
answers the same question — and a gate that read the room at plan time would be
deciding about a room that no longer exists.

### Turn-taking

Autonomy runs on a timer; conversation runs on turns. Reconciling the two is `autonomy_social.py`, and it is the reason Maxwell no longer walks into a conversation he is already in.

Before anything sends, the engine reads each room and returns a verdict on whether the floor is his:

| State | Meaning | Speak? |
| --- | --- | --- |
| `REPLYING` | the main reply path is generating here right now | no |
| `HOLDING` | he spoke last and nobody has answered yet | no |
| `HANDLED` | the live reply already covered the newest ping | no |
| `COOLDOWN` | he spoke here inside the quiet window | no |
| `BUSY` | other people are mid-exchange and not talking to him | no |
| `ADDRESSED` | someone is waiting on him and nothing has answered | yes |
| `OPEN` | ordinary room, nobody mid-thought | yes |
| `IDLE` | quiet for a while | yes |

The verdict is used twice on purpose: it is rendered into the planner prompt as a `CONVERSATION FLOOR` section so the model can choose freely among the rooms that are actually his, and it is re-checked against live state immediately before the send, because the plan is seconds stale by then and rooms move.

**This gates speaking only.** Research, memory writes, goal work, and reflection are never blocked by it — the point is to constrain timing, not initiative. Restraint that can be computed lives in code; the prompt is left free.

The same gate honours an open sleep window. The live reply path has always refused to answer while `,sleep` is set, telling people "max is sleeping, back in Xm" — but nothing checked it on the autonomy side, so the tick would post into a channel or DM someone while that notice was still standing. It is speech-only there too: asleep, he still thinks, remembers and plans.

| Control | Default | Meaning |
| --- | --- | --- |
| `autonomy_floor_enabled` | `true` | Enforce turn-taking. Off = planner still sees the read, `execute()` stops acting on it. Debugging only. |
| `autonomy_floor_cooldown_seconds` | `90` | Quiet window after his own last line before an unprompted new one. Being addressed bypasses it. |
| `autonomy_floor_hold_release_seconds` | `1800` | How long he keeps holding the floor after speaking into silence. |
| `autonomy_floor_mid_flow_seconds` / `_messages` | `45` / `3` | What counts as other people mid-exchange. |
| `autonomy_floor_idle_seconds` | `600` | Silence after which a room reads as idle rather than active. |

`autonomy_recent_reply_block_seconds` is the older single-purpose knob for the same idea. It still works and is honored as a minimum for the cooldown, so an existing tuned value is never silently shortened.

Manage these in the dashboard under **Autonomy → Turn-taking**.

Note that these windows are conversational, not mechanical: they are deliberately independent of `autonomy_interval_seconds`, because how long it is polite to wait before speaking again is a property of the room rather than of how often Maxwell wakes up.

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
- Features land continuously.

If you're running via PM2 (recommended), a push to main followed by a restart gives you the latest rolling update immediately.

Before you push, run the checks:

```bash
python3 -m pytest -q     # test suite
python3 doctor.py        # config/feature sanity
```

> Removed along the way: four never-wired "safety primitive" modules
> (`approval.py`, `attention.py`, `event_dispatch.py`, `memory_controls.py`,
> plus `docs/ARCHITECTURE_SAFETY.md` and their test file) — they had tests and
> a design doc and nothing ever imported them, while the gates that actually
> run live elsewhere: `Tool.is_destructive` + the taint check in `bot.py` for
> destructive tools, `autonomy_social.py` / `watch_policy.py` for unsolicited
> speech, and `rag_memory.py` for memory. Also removed: the Intel engine (commit `d455e4b`; the `,intel`
> commands and the `intel_enabled` / `intel_interval_seconds` control keys
> are stripped from `bot_control.json` at load), `memory.py`,
> `context_cleanup.py` (its `context_cleanup_*` control keys are stripped the
> same way; the `/api/context_cleanup/*` routes stay as no-op stubs so
> external callers do not 404), and the OpenCode sub-agent backend. RAG
> vector memory handles memory upkeep, and `sub_agent` now runs inside
> Maxwell itself (in its own Docker sandbox). The `autonomy` engine still writes fresh facts into
> long-term memory on its own cadence.

## Dashboard / API

The API server (`api/api_server.py`) serves a dashboard and admin interface.

- All API/data requests require HTTP Basic auth with `MAXWELL_ADMIN_USER` / `MAXWELL_ADMIN_PASSWORD`, except `OPTIONS` preflight and `POST /api/login`.
- `POST /api/login` is exempt from middleware; credentials are validated by the handler and rate-limited.
- The admin HTML can be served publicly, but it will not load data or mutate anything until credentials are supplied.

Control state moves over three routes. `GET /api/control` returns the live
control set — the persisted `data/bot_control.json` merged over
`DEFAULT_CONTROL` and run through the same sanitizer a write goes through, so
every key comes back even if nobody has ever set it. `PUT /api/control` takes a
partial object, ignores keys that are not in `DEFAULT_CONTROL`, clamps each
value to its documented range, and echoes the sanitized result; the dashboard
re-renders from that echo, so anything the server adjusted is visible
immediately. `DELETE /api/control` resets everything to defaults.

`control_defaults.py` is the single source of truth for those keys — the bot and
the API both import it, and the dashboard's Controls panel renders one input per
key with ranges that mirror the server-side clamp. A key added there with no
input in the panel is listed in that panel's "Not surfaced" card rather than
quietly going missing.

Read-only routes worth knowing: `GET /api/rag/memory` is aggregate vector
counters, `GET /api/rag/ltm` is the long-term-memory rows, `GET
/api/rag/entities` is the global per-user roster (`?user_id=` for one person
with their facts), and `GET /api/subagents` is live and recent sub-agent runs
(`?run_id=` for one). The sub-agent event bus lives in the bot process, so that
last one reads `data/subagent_runs.json`, which the bot rewrites as runs start,
step and finish.

The dashboard loads every panel's data independently (`Promise.allSettled`), so
one failing endpoint degrades that panel and names itself in the header instead
of blanking the page.

Static files (`web/index.html`, `web/admin/index.html`) should be copied to a web root. Reverse proxy `/api/*` and `/data/*` to `MAXWELL_API_HOST:MAXWELL_API_PORT`. See `examples/Caddyfile.example`.

## Security

- Never commit `.env`, `data/`, logs, PM2 dumps, or generated sites.
- Set real values for `MAXWELL_ADMIN_USER` and `MAXWELL_ADMIN_PASSWORD`. The API does not persist or bootstrap credentials.
- Generated bot sites serve arbitrary HTML. Host them on a separate origin from admin pages to prevent credential theft via XSS.
- The shell tool runs `bash -lc` inside the `maxwell-shell` Docker container (`docker/Dockerfile`), not on the host. By default that container is isolated: bridge network, `--cap-drop ALL` (plus a small add-back set), `no-new-privileges`, 4 GB / 2 CPU / 1024 pids, no docker socket, and no host filesystem — only `shelldocker/` is bind-mounted as its working directory at `/home/maxwell`. Setting `MAXWELL_SHELL_FULL_HOST=true` deliberately drops that wall: host network plus `/:/host:rw`, which is documented root-equivalent access for admins. The sub-agent runs under the same isolation: one throwaway container per run, off the same image, with only that run's scratch workspace mounted — so the bot's `.env` and source are not reachable from code it writes. `SUBAGENT_SANDBOX=host` opts out explicitly; with the default and no Docker daemon the tool errors rather than silently running on the host. Both tools are on by default; set `ENABLE_SHELL=false` (which also turns off `sub_agent`) to withhold that access entirely. The bot logs a warning at startup whenever shell is enabled.
- Running `shell` needs a working Docker daemon the bot user can reach. Without one the tool reports the failure rather than silently falling back to the host. `python3 doctor.py` tells you which side you are on.
- `DISABLE_TAINT_GATE=false` (the default) makes `shell` require an out-of-band `,confirm` on any turn that read fetched web content — the second line of defence against indirect prompt injection. Only disable it on a single-user install you fully trust.

## License

MIT. See `LICENSE`.

## Why am I doing this?

Just for fun idk you will see ALOT of ai slop and very specific stuff just made for my code and model so some things like audio recognition and video is for gemini and my website stuff and ect will not work for you sooo uhh yeah (your problem not mine if you have things that will help everyone like universal model selector for like adding models that have video support or dont ect please do a pull request thanks!)
