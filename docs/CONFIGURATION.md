# Maxwell configuration reference

Maxwell has two configuration layers:

1. `.env` for provider endpoints, credentials, identity, paths, feature availability, and startup behavior.
2. Runtime controls in `DATA_DIR/bot_control.json`, editable through the authenticated operator API and restricted maintenance controls. The shared Maxwell persona is locked in code.

Keep `.env` and `data/` private.

## Primary AI settings

The easy installer and `.env.simple.example` use provider-neutral names:

| Variable | Purpose |
|---|---|
| `AI_BASE_URL` | Primary OpenAI-compatible API base URL |
| `AI_MODEL` | Primary chat model |
| `AI_API_KEY` | Primary API key; blank is normal for local endpoints that do not require one |

The runtime reads these names directly; legacy `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `OLLAMA_API_KEY` remain supported for existing installations. Non-empty `AI_BASE_URL` and `AI_MODEL` take precedence over legacy settings. An explicitly set `AI_API_KEY`, including a blank value for a local endpoint, takes precedence over a legacy key.

All advanced generation, retry, fallback and vision controls use `AI_*`. Deprecated `OLLAMA_*` inputs remain supported; see [PROVIDERS.md](PROVIDERS.md).

For an older install:

```bash
python3 scripts/migrate_ai_env.py .env
```

Do not set conflicting values in both namespaces.

## Core environment values

| Variable | Required? | Purpose |
|---|---:|---|
| `DISCORD_BOT_TOKEN` | Yes | Official bot token from the Discord Developer Portal. User/self-bot tokens are not supported. |
| `DISCORD_TOKEN` | Deprecated alias | Used only when the official bot-token setting is empty. |
| `AI_BASE_URL` / legacy `OLLAMA_BASE_URL` | Yes | Primary OpenAI-compatible endpoint. |
| `AI_MODEL` / legacy `OLLAMA_MODEL` | Yes | Primary chat model. |
| `AI_API_KEY` / legacy `OLLAMA_API_KEY` | Sometimes | Provider key; blank can be valid for local endpoints. |
| `MAXWELL_OWNER_IDS` | Strongly recommended | Comma-separated Discord IDs authorized for owner/admin features. |
| `MAXWELL_ADMIN_USER` | Optional | Operator API HTTP Basic username; defaults to `admin`. |
| `MAXWELL_ADMIN_PASSWORD` | Strongly recommended | Operator API HTTP Basic password. A blank value leaves the API unavailable. |
| `MAXWELL_HOST_BIND` | Installer-managed | Absolute host checkout path used by sibling containers. |
| `MAXWELL_SITE_DIR` | Optional | Generated-site directory. Leave unset for the repository default or use an absolute host path. |
| `MAXWELL_PUBLIC_BASE_URL` | Optional | Public origin for generated sites. Keep it separate from the operator API origin. |
| `DISCORD_CLIENT_ID` | Optional | Discord application ID for bot invite URLs; falls back to the bot ID. Not a browser login setting. |

See [`.env.example`](../.env.example) for the complete advanced environment reference.

## Generated-site custom backends

`ENABLE_CREATE_SITE` gates static sites, the built-in KV API, and `site_server`.
`backend=true` on `create_site` enables the KV API only. Use `site_server` for
custom Python routes, authentication, server-side secrets, or WebSockets.
The standard stack is FastAPI + Uvicorn (one worker, no reload) + SQLite.
Backend code and lifecycle access require the site owner or a Maxwell admin.

Custom backends require Linux, Docker host networking, cgroup v2 with the
systemd driver, and the configured gVisor `runsc` runtime. Run
`sudo bash scripts/setup_site_host.sh` on the Docker host before deploying.
The controller needs the read-only `/etc/maxwell-sites` policy and
`/sys/fs/cgroup` mounts in the Linux Compose file. Docker Desktop bridge
deployments do not support this host-local proxy configuration.

The shared site pool is capped at 2 CPU cores, 4 GiB RAM, no swap, and 2,048
tasks. Each backend has 256 MiB RAM, 0.5 CPU, and bounded processes, temporary
storage, and logs. At most 64 managed backends can exist. These are ceilings,
not reserved allocations; reaching the shared memory cap can terminate a
backend rather than consume the host's remaining RAM.

The public proxy admits at most 128 active requests globally and 32 per site,
including WebSockets and SSE streams. Excess requests receive HTTP 503 with
`Retry-After: 1`; request-rate and upload limits still apply.
Only `/data` persists across backend restarts. Keep SQLite databases there,
use parameterized SQL and short transactions, and close connections.
Persistent application data is not disk-quota limited; monitor disk space.
Source files and environment secrets live outside the public static root.

Existing containers enter the protected pool when restarted through
`site_server`. This causes brief per-site downtime without deleting their
source, secrets, or databases. See [SITE_BACKEND_MIGRATION.md](SITE_BACKEND_MIGRATION.md).


## Identity and ownership

| Variable | Default | Purpose |
|---|---|---|
| `BOT_NAME` | `Maxwell` | Bot/persona name where a live Discord nickname does not override it. |
| `CREATOR_NAME` | blank | Optional creator label. |
| `CREATOR_ID` | blank | Optional creator Discord ID. |
| `MAXWELL_OWNER_IDS` | blank | Comma-separated owner/admin Discord IDs. |
| `MAXWELL_USER_ID` | blank | Optional configured bot user ID. |
| `COMMAND_PREFIX` | `,` when unset | Internal compatibility prefix used by slash-command handlers; public text-prefix commands are retired. |
| `BOT_BIRTHDAY` | `2026-05-21` | ISO persona birthday. |
| `BOT_INVITE_URL` | blank | Optional public invite URL. |
| `MAXWELL_USAGE_URL` | blank | Optional provider usage/quota endpoint. |

Blank IDs do not grant implicit ownership.

## Discord slash commands

`/config` opens a private settings home with four buttons:

- **Personality**: edit the style of your replies or restore the default.
- **Language**: choose Automatic, English, Spanish, or enter another language.
- **Replies & context**: set visibility and answer detail directly, then open
  response mode, web research or recent-message settings as needed.
- **AI connection**: optionally connect a personal provider with an API key;
  see [BYOK.md](BYOK.md) for setup and advanced controls.

Personal style and language follow you across channels, servers and DMs. App
request defaults apply to `/maxwell` and message actions, and explicit command
options can override them. Every screen has **Back** and **Close**. Changes
show saved feedback, and the menu expires after five minutes.

Members with Manage Server also see a **Server settings** tab for response
channels, allowed tools, extra tools, moderation, progress updates and ticket
greetings. Selecting a tool group **allows** its tools. The channel picker
supports all server text channels, with a separate **Allow all channels**
button. Configured application owners see **Bot controls** for diagnostics,
global tools, automatic activity and control reloads. Permissions are rechecked
on every action; retired message-limit controls are no longer advertised.

`/cancel` cancels your active request in the current conversation. The former
prefix commands and standalone personality/owner settings commands are retired.
Maxwell's shared system prompt cannot be edited through `/config`.

Usage is counted in messages over a rolling window, and that is the only usage measure. Fresh installs leave `message_quota_enabled` off, so self-hosted runtimes admit unlimited messages until an operator turns a quota on. There is no billing, plan, token quota, or paid tier.

## Runtime controls

Defaults live in `control_defaults.py`. `bot_control.json` overrides them at runtime.

Live replies have no bot-wide request, inference, or provider connection cap,
including primary and fallback models. Independent channels and servers start
concurrently. Legacy `ai_concurrency` and `MAX_PENDING_REPLY_REQUESTS` settings
are ignored, and saved message quotas are forced off. Turns in one conversation
retain their order with no backlog admission cap. Tool dispatch has no shared
budget, while shell isolation, search-worker bounds, request deadlines, Discord
rate limits, and upstream provider limits still apply. Removing local admission
caps does not establish a measured production capacity.

Credentialed Git uses a private disk volume per user/repository for sanitized
object caching; unchanged packs are reused across operations. Working snapshots
are deleted after commands, stale snapshots are cleaned on the next operation,
and the cache retains only current objects within the 4 GiB limit. Authentication
files stay on the container's ephemeral `/tmp` rather than the persistent cache.

Frequently used controls include:

| Control | Purpose |
|---|---|
| `bot_enabled` | Master live-response switch |
| `tools_enabled` | Master tool-layer switch |
| `native_tool_calls` | Prefer OpenAI-style provider-native tool calls |
| `disabled_tools` | Per-tool deny list |
| `require_direct_response` | Require an answer/acknowledgement for eligible direct requests |
| `respond_to_edited_mentions` | Allow a newly added direct mention to start one request |
| `max_tool_iterations` | Tool-loop iteration cap |
| `tool_iteration_timeout_seconds` | Tool-loop timeout |
| `prompt_context_budget` | Approximate prompt/context budget |
| `memory_context_budget` | Approximate memory contribution budget |
| `live_max_output_tokens` | Maximum output tokens for a live text response (default 4096) |
| `message_quota_limit` | Free messages per rolling window (default 300) |
| `message_quota_window_seconds` | Rolling window length (default 18000, five hours) |
| `message_quota_enabled` | Forced off for unlimited request admission |
| `store_memory` | Conversation-memory storage switch |
| `autonomy_enabled` | Runtime autonomy switch |
| `enable_night_fallback` | Night-window fallback routing when configured |

The authoritative set and valid ranges are in `control_defaults.py` and `api.state._sanitize_control`. Prefer the authenticated operator API or `/maintenance` to editing `bot_control.json` while Maxwell is running. There is no browser admin frontend or Discord browser OAuth login.

## Prompt size and provider caching

`prompt_context_budget` and `memory_context_budget` are character budgets, not exact model-token counts. The final prompt limit is soft: complete historical transcript blocks may be removed, but authorization/system instructions, live input, and native tool-call/result records are preserved rather than clipped mid-rule.

Tool guidance is conditional on enabled plugins, available tools, and request scope. Component `token_budget` values are advisory; they never truncate an instruction.

Reusable instructions and authorized history precede volatile requester/time/retrieval context. This permits prefix-cache reuse when the provider supports it and the prefix remains byte-identical; changing model, tool access, or configuration can invalidate that prefix. Maxwell does not promise a cache hit or a provider-specific token saving.

Prompt memory budget is allocated to recent history. REM, RAG, embedding workers, entity/graph retrieval, context extraction, automatic summaries, delegated workers, and automatic code repair are removed from the live runtime. Saved settings cannot reactivate them.


## Message reliability

Directed inbound work is journaled in `DATA_DIR/inbound_requests.sqlite3` with lifecycle state such as received, queued/deferred, running, failed, and delivered. The journal records IDs/state/timing rather than duplicating full private message content.

Relevant controls include `live_turn_timeout_seconds`, `inbound_retry_attempts`, and `inbound_retry_delay_seconds`. Maxwell deliberately avoids blind replay after a tool or Discord send may already have taken effect.

## Conversation history

`conversation_memory.py` stores scoped recent messages in SQLite without an embedding endpoint, vector search, graph retrieval, local model, or recovery worker. The existing `maxwell_rag.db` filename and schema are retained so upgrades preserve conversation history and archived facts. Archived vectors remain on disk for export and are never loaded into live prompts.

`ENABLE_RAG` and legacy retrieval controls cannot enable RAG. NumPy is no longer a runtime dependency. Old vector modules and their regression fixtures remain available for offline migration work only. `doctor.py --probe` checks chat endpoints.

## Optional/background features

Many `ENABLE_*` environment switches accept `auto`, `true`, or `false`. Examples include `ENABLE_AUTONOMY` and `ENABLE_SHELL`; `ENABLE_RAG` is permanently disabled.

Simple installs keep token-spending background loops off by default. The exact dependency detection is implemented in `config.py` and reported by `doctor.py`.

## App-command behavior

For `/maxwell`, fast answers use the original deferred interaction. A tool-backed or >10-second command updates the temporary interaction status with tool names, removes that status on completion, and sends the final result as a follow-up. Textual `/maxwell` follow-ups are rendered as branded embeds unless the response already contains an explicit rich payload.

## Tool safety behavior

Tools marked destructive are blocked when the current turn has been tainted by fetched/web content. A fresh user message starts a clean turn. There is no manual confirmation command that overrides a tainted destructive action.

See [../SECURITY.md](../SECURITY.md) for the Docker-daemon trust boundary and deployment warnings.

## Reconfigure

For a simple/easy install, edit the friendly `.env` values and restart:

```bash
cd ~/maxwell
nano .env
./run.sh -d
```

For the full wizard:

```bash
cd ~/maxwell
./install.sh --local --reconfigure
```

Then validate:

```bash
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```
