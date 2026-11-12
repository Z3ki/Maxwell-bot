# Maxwell configuration reference

Maxwell has two configuration layers:

1. `.env` for provider endpoints, credentials, identity, paths, feature availability, and startup behavior.
2. Runtime controls in `DATA_DIR/bot_control.json`, editable through the authenticated operator API and restricted maintenance controls. The shared Maxwell persona is locked in code.

Keep `.env` and `data/` private.

## Music

Music uses the existing Lavalink connection settings; playback is controlled through conversation, not music panels or slash commands. No music UI environment variables are required. See [MUSIC.md](MUSIC.md) for setup and permissions.

PyNaCl and `davey` are optional for native discord.py voice, which Maxwell does not use for its Lavalink music or TTS message attachments. Missing-native-voice warnings do not imply that those features are unavailable; see [MUSIC.md](MUSIC.md).

## Live webhook testing

`LIVE_TEST_WEBHOOK_IDS` and `LIVE_TEST_CHANNEL_IDS` are comma-separated Discord
IDs, both empty by default. When both contain a message's webhook ID and channel
ID, respectively, that webhook may receive replies even when `reply_to_bots` is
off. Other bot and webhook policy is unchanged. These are independent sets,
so each listed webhook is allowed in every listed channel. Use only temporarily
in an isolated test bot and remove both settings after testing.

## Usage metrics

Operational metrics use `DATA_DIR/usage_metrics.sqlite3` (SQLite WAL). The
`USAGE_METRICS_RETENTION_DAYS` setting defaults to 90 days (2–3650), and
`USAGE_METRICS_ID_RETENTION_DAYS` defaults to 7 days (1–365, capped at one day
less than aggregate retention). Invalid settings use the defaults. Pruning runs on writes, reads,
and hourly while the metrics worker is active.

Daily aggregates contain event counts, metadata dimensions, numeric totals and
bounded latency histograms. Average latency uses recorded duration totals at
microsecond resolution; overall, per-tool and per-model/provider/route p95 values
are bucket upper bounds, not exact percentiles. Model reports identify the
configured model, the lowercase endpoint hostname as provider, and the routing
role (`primary`, `fallback`, `vision`, or `worker`) separately. If the hostname
is unavailable, the provider falls back to the role. Unconfigured model names
are recorded as `other`; personal BYOK models, hosts and endpoints are never
recorded and use `byok` for all three labels. Older model/provider groups remain
in reports. Daily 256-register cardinality sketches estimate active users and
servers, including users without message storage enabled. Daily sketches can be
merged for a period, but estimates
have statistical error. They contain no per-user rows; an individual contribution
cannot be subtracted and the sketches should not be described as mathematically
anonymous, especially for small populations.

The separate short-lived membership table contains salted hashes, not plaintext
identifiers. User hashes rotate daily and are recorded only when optional message
storage is explicitly enabled. Its opted-in distinct count is **user-days**, not
unique people across a period. Server membership counts cover only the shorter
membership retention window. Content, prompts and search queries are not metrics.
Privacy deletion removes user membership, not aggregate totals or sketch
contributions; the dashboard privacy response includes retained membership days.

Application owners can use `/stats` with an optional `days` argument. Operators
can also run `python scripts/stats.py --days 30` inside the container; `--json`
emits the complete report, including grouped calls, successes, failures, latency,
top commands, search counts, error types and token totals.
Writes are best effort: the bounded queue drops events when full, and pending
events can be lost on shutdown or storage failure.


## Primary AI settings

The easy installer and `.env.simple.example` use provider-neutral names:

| Variable | Purpose |
|---|---|
| `AI_BASE_URL` | Primary OpenAI-compatible API base URL |
| `AI_MODEL` | Primary chat model |
| `AI_API_KEY` | Primary API key; blank is normal for local endpoints that do not require one |

The runtime reads these names directly; legacy `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `OLLAMA_API_KEY` remain supported for existing installations. Non-empty `AI_BASE_URL` and `AI_MODEL` take precedence over legacy settings. An explicitly set `AI_API_KEY`, including a blank value, takes precedence over the legacy Ollama key; if the resolved key is blank, `OPENAI_COMPAT_API_KEY` is still used when set.

All advanced generation, retry, fallback and vision controls use `AI_*`. Deprecated `OLLAMA_*` inputs remain supported; see [PROVIDERS.md](PROVIDERS.md).

An independently configured operator worker may use Anthropic Messages via
`AI_WORKER_API_FORMAT=anthropic`; `auto` (the default) detects the
`api.anthropic.com` host and otherwise selects OpenAI-compatible Chat
Completions. `AI_WORKER_FALLBACK_API_FORMAT` independently selects `auto`,
`openai`, or `anthropic` for the worker's fallback. Set the format explicitly
for proxies whose host does not identify their protocol. Set
`AI_WORKER_FALLBACK_MODEL` for a dedicated fallback; its endpoint and key can
inherit from the worker only at the same endpoint. A different endpoint requires
its own key (or an explicitly blank key for a keyless endpoint). Personal BYOK
does not fall back to operator credentials. See
[worker configuration](PROVIDERS.md#independent-subagent-provider) for selection
and fallback details.

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
| `AI_BASE_URL` / legacy `OLLAMA_BASE_URL` | Optional | Primary OpenAI-compatible endpoint; defaults to `http://localhost:11434`. |
| `AI_MODEL` / legacy `OLLAMA_MODEL` | Yes | Primary chat model; there is no default model. |
| `AI_PREMIUM_MODEL` | Optional | Stronger chat model for an operator-granted premium plan. Blank keeps premium on `AI_MODEL`. |
| `AI_API_KEY` / legacy `OLLAMA_API_KEY` | Sometimes | Provider key; blank can be valid for local endpoints. |
| `MAXWELL_OWNER_IDS` | Strongly recommended | Comma-separated Discord IDs authorized for owner/admin features. |
| `MAXWELL_ADMIN_USER` | Optional | Operator API HTTP Basic username; defaults to `admin`. |
| `MAXWELL_ADMIN_PASSWORD` | Strongly recommended | Operator API HTTP Basic password. A blank value leaves the API unavailable. |
| `MAXWELL_HOST_BIND` | Installer-managed | Absolute host checkout path used by sibling containers. |
| `MAXWELL_SITE_DIR` | Optional | Generated-site directory. Leave unset for the repository default or use an absolute host path. |
| `MAXWELL_PUBLIC_BASE_URL` | Optional | Public origin for generated sites. Keep it separate from the operator API origin. |
| `DISCORD_CLIENT_ID` | Optional | Discord application ID for bot invite URLs; falls back to the bot ID. Not a browser login setting. |

See [`.env.example`](../.env.example) for the complete advanced environment reference.

## Diagnostic logging

`LOG_LEVEL` controls verbosity (default `info`). `LOG_FORMAT` selects `text`
(default) or `json`. Logs through the configured root handlers go to stdout
through warning and stderr for errors. Messages at INFO and above, including
legacy messages, are credential-redacted and capped at 300 characters with an
ellipsis when truncated. Exception diagnostics include the type, a similarly
redacted and capped exception message, and stack locations without source lines.
Call sites avoid logging message content, prompts and tool results at INFO and
above, but exception messages may contain external or user-supplied text and
redaction cannot recognize every secret. DEBUG messages are redacted and capped
at 200 characters, but can still contain private non-secret information; keep
all logs protected and enable DEBUG only when necessary.
Handlers attached separately to individual loggers and direct writes to streams
are not covered by this formatter.

Configured credential values of at least four characters are redacted even when
an explicitly secret setting contains only digits or looks like a boolean.
Token-count settings, webhook ID lists and non-secret feature flags are not
treated as credentials merely because their names contain `token` or `key`.

Set `LOG_FILE` to enable an additional rotating file. `LOG_FILE_MAX_BYTES`
defaults to 10485760 and is clamped to 1024–1073741824;
`LOG_FILE_BACKUP_COUNT` defaults to 5 and is clamped to 1–100. A newly created
file is private to the process owner. The configured maximum applies per file,
with the specified number of backups in addition to the active file. If the
optional file cannot be opened, stream logging remains available.

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


## Premium storage, public folders and schedules

Premium is an operator grant: run the restricted `/maintenance` command with
the **Grant premium allowance** action (`quota_premium`) and the user's Discord
ID in `key`; **Return user to free allowance** (`quota_free`) reverts it. These
limits apply to accounts on the `premium` plan. Free accounts keep the
shell, sites and everything else, with no persistent storage and no schedules.

| Variable | Default | Purpose |
|---|---|---|
| `MAXWELL_STORAGE_PREMIUM_MB` | `100` | Persistent `/storage` quota for a Premium account (max 1024). |
| `MAXWELL_STORAGE_FREE_MB` | `0` | Quota for free accounts. `0` means no `/storage`; files left from an earlier grant stay readable and deletable. |
| `MAXWELL_STORAGE_MAX_FILES` | `2000` | File-count cap per account. |
| `MAXWELL_SHELL_TIMEOUT` | `600` | Per-command shell limit in seconds (ceiling 900). |
| `MAXWELL_SHELL_PREMIUM_TIMEOUT` | `600` | Premium commands get at least this long; the ceiling still wins. |
| `MAXWELL_SHELL_IDLE_SECONDS` | `600` | Idle time before a sandbox is destroyed. `/storage` is saved first and restored on the next command. |
| `MAXWELL_SCHEDULE_MAX_PER_USER` | `10` | Saved schedules per account. |
| `MAXWELL_SCHEDULE_MIN_MINUTES` | `15` | Shortest allowed gap between runs. |
| `MAXWELL_SCHEDULE_DAILY_RUNS` | `48` | Runs per account per day across all schedules. |
| `MAXWELL_DASHBOARD_LINKS` | `1` | `/dashboard` one-time sign-in links. `0` turns link sign-in off. |
| `MAXWELL_USER_CONTENT_STREAMS` | `32` | Concurrent `/u/` file responses the API serves at once. |

Storage lives in `data/plugins/user_storage/` (one directory per Discord user
plus a SQLite index for handles). The shell mounts a quota-sized tmpfs at
`/storage`, restores it before each command and merges changes back after; a
bind mount would hand an untrusted guest an unquotaed host directory. Files a
subagent reports from `/workspace` are copied to `/storage/jobs/<job id>/`
when the job finishes, so the idle wipe can't take them.

Files under `/storage/public/` are served at `<MAXWELL_PUBLIC_BASE_URL>/u/<handle>/`
by the API (`api/user_content.py`) with a sandboxing Content-Security-Policy.
Private files can be shared with expiring signed links at `/u/_s/...`. Route
`/u/*` on the public host to the API, for example in Caddy:

```caddy
handle /u/* {
	reverse_proxy 127.0.0.1:8765
}
# Dev bot under /dev on the same host:
handle /dev/u/* {
	uri strip_prefix /dev
	reverse_proxy 127.0.0.1:8766
}
```

Schedules live in `data/plugins/schedules/schedules.sqlite3`. A runner checks
every 20 seconds and starts each due schedule as a background subagent job, so
schedules also need Premium task workers (`premium_task_workers_enabled`).
`get_configuration` returns the resolved shell, storage, schedule and site
limits for the requester.

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

Blank IDs do not grant implicit ownership.

## Discord slash commands

`/config` opens a private settings home with five buttons:

- **Personality**: edit the style of your replies or restore the default.
- **Language**: choose Automatic, English, Spanish, or enter another language.
- **Replies & context**: set visibility and answer detail directly, then open
  response mode, web research or recent-message settings as needed.
- **AI connection**: optionally connect a personal provider with an API key;
  see [BYOK.md](BYOK.md) for setup and advanced controls.
- **Privacy**: choose whether your message content is stored. Storage defaults
  to on when a user has no saved preference; turning it off stops new
  saves and deletes stored messages, replies to you, tool records from those turns, and facts already stored for your Discord account.
  A request you send is still answered and is still sent to the AI provider.

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

The message allowance is counted over a rolling window, separately from operational metrics. Fresh installs leave `message_quota_enabled` off, so self-hosted runtimes admit unlimited messages until an operator turns a quota on. There is no billing, checkout, price, or token quota. When the quota is on, the free allowance defaults to 150 messages per five hours. An operator can grant premium, which raises that allowance to at least 500 messages (or the higher configured free limit) in the same window and uses `AI_PREMIUM_MODEL` when that variable is set.

### A loaded tool is missing in one server

Plugin loading does not imply that a server's model can use its tools. A saved
`guild_plugin_overrides["<guild_id>"]["chess"] = false` hides `chess_start`,
`chess_move`, `chess_state` and `chess_resign` from that server's tool schemas
and chess instructions even when startup logs say the chess plugin loaded.

To restore chess, open `/config` → **Server settings** → **Extra tools** in
the affected server and include **Chess** among the enabled plugins. Preserve
the other selections. An operator can instead update that server's `chess`
override to `true` through the authenticated control API, preserving other
guilds and plugin overrides. Controls reload without a restart.

If tools are still missing, check the master `tools_enabled` switch,
`disabled_tools`, server tool-group restrictions and explicit per-user plugin
denials. A missing `python-chess` dependency prevents chess from loading at all;
installing it in a shell sandbox does not change the bot's Python environment.


## Runtime controls

Defaults live in `control_defaults.py`. `bot_control.json` overrides them at runtime.

Live replies have no bot-wide request, inference, or provider connection cap,
including primary and fallback models. Independent channels and servers start
concurrently. Legacy `ai_concurrency` and `MAX_PENDING_REPLY_REQUESTS` settings
are ignored. A saved `message_quota_enabled` value is enforced. Turns in one conversation
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
| `max_tool_iterations` | Tool-loop iteration cap (default 50) |
| `tool_iteration_timeout_seconds` | Tool-loop timeout |
| `prompt_context_budget` | Approximate prompt/context budget in chars (default 96000) |
| `memory_context_budget` | Most chars of channel history per turn (default 48000) |
| `memory_history_messages` | Channel history window in messages (default 50; ordinary eviction is chunked) |
| `live_max_output_tokens` | Maximum output tokens for a live text response (default 4096) |
| `message_quota_limit` | Free messages per rolling window (default 150) |
| `message_quota_window_seconds` | Rolling window length (default 18000, five hours) |
| `message_quota_enabled` | Enforce message quota when enabled (default off) |
| `store_memory` | Conversation-memory storage switch |
| `enable_night_fallback` | Night-window fallback routing when configured |
| `conversation_watch_enabled` | Allow eligible unmentioned follow-ups after a directed exchange or Maxwell's post (default false) |
| `conversation_watch_seconds` | Base watch window in seconds (default 60; zero disables watch) |
| `conversation_watch_debounce_seconds` | Baseline quiet interval for soft follow-ups (default 1 second) |
| `conversation_watch_pressure` | Threshold for considering soft follow-ups (default 0.55; higher is stricter) |

Watch windows and debounce intervals adapt to room activity; the model may still
choose silence. Direct mentions and replies are not delayed by the soft-follow-up
debounce. Directed requests cannot use `wait` before a visible response. The
`wait` tool pauses a batch for up to 10 seconds (default 2), does not schedule
another model turn, and cannot reopen a completed turn. Ambient waits remain
available; a send ends the batch except for consecutive reply-burst sends.

Task workers are enabled by default and `subagents_require_premium` defaults to
false. Schedules and storage remain plan-gated. Worker selection and timeout
settings are described in [PROVIDERS.md](PROVIDERS.md#independent-subagent-provider).

The authoritative set and valid ranges are in `control_defaults.py` and `api.state._sanitize_control`. Prefer the authenticated operator API or `/maintenance` to editing `bot_control.json` while Maxwell is running. The optional user dashboard uses Discord OAuth; see [DASHBOARD.md](DASHBOARD.md).

## Prompt size and provider caching

`prompt_context_budget` and `memory_context_budget` are character budgets, not exact model-token counts. History gets what is left of `prompt_context_budget` after the output reserve (a quarter of the budget), the ~20k-char system prompt and a per-turn tail reserve (an eighth of the budget, at least 8000), capped by `memory_context_budget`. Below roughly 96000 the system prompt crowds history out and the bot logs `History squeezed`; keep `memory_context_budget` near half of `prompt_context_budget`. The final prompt limit is soft: complete historical transcript blocks may be removed, but authorization/system instructions, live input, and native tool-call/result records are preserved rather than clipped mid-rule.

On loading older saved controls, the former default `memory_history_messages`
value of 40 is migrated to 50 once; other saved values are preserved. The
internal `_history_default_50_migration` marker records the migration and its
changes, so a later intentional choice of 40 is not migrated again. The
personal-app default is also 50; explicitly saved personal choices remain exact.
With sufficient character budget, ordinary history retains 41–50 recent chat
messages, dropping ten-row chunks; tool records, pinned replies and optional
recall are budgeted separately. Character limits can reduce history further;
the live request is retained.
Native tool calls begin with a compact core and an authorized catalog. The model
can call `load_tools` for additional schemas during that turn; availability and
execution remain subject to the normal permission and plugin gates.
`MAXWELL_LAZY_TOOLS=false` (default `true`) sends every authorized schema on
each call instead. Some providers only cache prompts above a minimum size: in
testing, Gemini through Antigravity reported no cached tokens for ~10k-token
lazy prompts but cached ~18k of ~20.6k tokens with full schemas.
When a chat has no stored history, a fixed "no earlier messages" user
message separates the fixed system message from the per-turn block, so gateways
that merge leading system messages do not fold the timestamp into the prefix.

Tool guidance is conditional on enabled plugins, available tools, and request scope. Component `token_budget` values are advisory; they never truncate an instruction.

Reusable instructions and authorized history precede volatile requester/time/retrieval context. This permits prefix-cache reuse when the provider supports it and the prefix remains byte-identical; changing model, tool access, or configuration can invalidate that prefix. Maxwell does not promise a cache hit or a provider-specific token saving.

Prompt memory budget is allocated to recent history. REM, RAG, embedding workers, entity/graph retrieval, context extraction, automatic summaries and automatic code repair are removed from the live runtime. Saved settings cannot reactivate them. User-requested subagent jobs are separate from those removed systems.


## Message reliability

Directed inbound work is journaled in `DATA_DIR/inbound_requests.sqlite3` with lifecycle state such as received, queued/deferred, running, failed, and delivered. The journal records IDs/state/timing rather than duplicating full private message content.

Relevant controls include `live_turn_timeout_seconds`, `inbound_retry_attempts`, and `inbound_retry_delay_seconds`. Maxwell deliberately avoids blind replay after a tool or Discord send may already have taken effect.

`live_turn_timeout_seconds` bounds the entire turn, not each provider/tool call.
Its default is 900 seconds, enough for multi-step site creation and
verification; `ai_timeout_seconds` and `tool_iteration_timeout_seconds` are
larger but cannot outlast it. Keep network calls independently bounded; raising
the turn budget does not make blocked sandbox destinations reachable.

## Conversation history

`conversation_memory.py` stores scoped recent messages in SQLite without an embedding endpoint, vector search, graph retrieval, local model, or recovery worker. The existing `maxwell_rag.db` filename and schema are retained so upgrades preserve conversation history and archived facts. Archived vectors remain on disk for export and are never loaded into live prompts.

`ENABLE_RAG` and legacy retrieval controls cannot enable RAG. NumPy is no longer a runtime dependency. The old vector modules and their fixtures were deleted; nothing reads embedding settings anymore. `doctor.py --probe` checks chat endpoints.

## Optional/background features

Many `ENABLE_*` environment switches accept `auto`, `true`, or `false`. Examples include `ENABLE_SHELL` and `ENABLE_CREATE_SITE`; `ENABLE_RAG` is permanently disabled.

Simple installs keep token-spending background loops off by default. The exact dependency detection is implemented in `config.py` and reported by `doctor.py`.

## App-command behavior

For `/maxwell`, fast answers use the original deferred interaction. A tool-backed or >10-second command updates the temporary interaction status with tool names, removes that status on completion, and sends the final result as a follow-up. Textual `/maxwell` follow-ups are rendered as branded embeds unless the response already contains an explicit rich payload.

## Tool safety behavior

Tools marked destructive are blocked when the current turn has been tainted by fetched/web content, except `shell`, which remains available on purpose so Maxwell can follow up on a URL. Shell still runs only inside the per-tenant gVisor sandbox. A fresh user message starts a clean turn. There is no manual confirmation command that overrides a tainted destructive action.

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
