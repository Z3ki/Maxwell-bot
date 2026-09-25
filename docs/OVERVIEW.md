# Maxwell architecture overview

Maxwell is an official Discord bot powered by an OpenAI-compatible chat API. It accepts Discord text plus images, audio, video, files, embeds, replies, and app-command/context-menu requests, then runs a tool-capable model loop with memory, background jobs, and owner/admin controls.

Maxwell expects an official bot token from the Discord Developer Portal. Enable the privileged gateway intents **Message Content**, **Server Members**, and **Presence**.

## High-level flow

```text
Discord bot / app commands
        │
        ▼
     bot.py (transport + turn loop)
        │
        ▼
 maxwell_core (plugins, tools, prompts, hooks)
        │
        ├── plugins/<feature>/     tools, jobs, prompt slices
        ├── providers.py           OpenAI-compatible adapter
        ├── rag_memory.py          SQLite/RAG memory
        ├── knowledge_graph.py     entity/relationship memory
        ├── autonomy.py / rem.py   background host loops
        └── api/ + web/            dashboard
```

## Main modules

| Path | Current role |
|---|---|
| `bot.py` | Discord client, message ingestion, multimodal handling, tool-call loop, command handling, delivery/reliability integration. |
| `maxwell_core/` | Plugin host, tool registry, prompt manager, hook bus, service container. |
| `plugins/` | Feature packages (Discord tools, web, sites, chess, extras, …). |
| `plugin_manager.py` | Compatibility façade over `maxwell_core`. |
| `config.py` | Loads `.env`, validates core settings, resolves optional feature flags. |
| `providers.py` | OpenAI-compatible chat/streaming provider client, endpoint normalization, retries/fallbacks. |
| `bot_tools.py`, `tool_registry.py`, `tool_schemas.py`, `tools.py` | Compatibility re-exports, reasoning traces, and schema helpers. Implementations live in `plugins/*/impl.py`. |
| `rag_memory.py` | `RAGMemoryManager`: SQLite-backed/vector-backed memory for channel messages, long-term facts, and scoped context. |
| `knowledge_graph.py` | Entity/relationship knowledge memory. |
| `context_budget.py` | Prompt/context budget helpers. |
| `autonomy.py` | Optional self-directed background action engine. |
| `rem.py` | Optional REM-style background memory consolidation. |
| `jobs.py` | Background jobs and long-running work. |
| `message_pipeline.py` | Message-processing pipeline helpers. |
| `user_install.py` | Discord user-install/app-command and context-menu support. |
| `plugins/maxwell_extras/` | Owner control panel, app-command progress, rich `/maxwell` output. |
| `api/api_server.py`, `api/state.py`, `api/storage.py` | Dashboard/admin API, sanitized controls, persisted admin state. |
| `web/` | Dashboard frontend. |
| `site_server.py`, `site_backend.py` | Generated-site serving/backend runtime integration. |
| `docker/` | Bot image (`maxwell.Dockerfile`), shell sandbox, and site-runtime images. |
| `docker-compose.yml` | Linux Compose file (host network). `docker-compose.bridge.yml` is the Docker Desktop variant. |
| `discord_threads.py` | Discord thread create/control tools and the brief injected into thread turns. |
| `doctor.py` | Installation/configuration report; `--probe` calls the configured endpoints. |
| `requirements.txt` | Core Python packages required to start Maxwell. |
| `requirements-optional.txt` | Optional packages for web search, voice, and TTS features. |

Historical host/PM2 files may remain for compatibility/migration, but Docker is the supported deployment model.

## Tool calling

`native_tool_calls` is enabled by default in current runtime controls. When the provider supports OpenAI-style tool calls, Maxwell uses the provider's structured `tool_calls`. A compatibility path remains for models/endpoints that do not produce native calls correctly.

Plugins register tools through `maxwell_core`. Schemas are still normalized in `tool_schemas.py`, and the host dispatches calls through the live registry. Long-running/background jobs use the same tool/runtime concepts with their own budgets and scheduling.

Tools marked destructive are fail-closed when the current turn has been tainted by fetched/web content. A new user turn starts clean; there is no manual confirmation command to override a tainted destructive action.

## Memory

Current memory includes:

- `RAGMemoryManager` in `rag_memory.py`, backed by SQLite and an OpenAI-compatible embedding endpoint, published to plugins as the `memory` service.
- Channel/message memory and long-term fact storage.
- Scoped shared/cross-context memory with visibility controls.
- Entity/relationship memory through `knowledge_graph.py`.
- Optional REM-style consolidation.
- Prompt/context budgeting before data is injected into a model turn.

RAG availability is controlled through configuration/feature switches. Embedding failures should not be confused with the primary chat provider; `doctor.py --probe` reports the configured provider/embedding probes.

## Discord app surfaces

Maxwell supports the normal bot conversation path plus Discord app-command/user-install surfaces.

- `/maxwell` is the personal app-command surface when user install is enabled.
- `/config` opens a private settings menu for personal defaults and server settings to authorized server administrators.
- `/personality` stores a personal reply-style preference.
- `/diagnostics` and `/maintenance` are restricted to configured Maxwell developers.
- `/image`, `/chess`, `/checkers`, `/moderation`, `/memory`, and `/reminder` provide focused request paths.
- Message/user context-menu actions are registered through the user-install layer.

The `maxwell_extras` plugin adds the current `/maxwell` presentation behavior: textual slash-command replies use clean branded embeds. Fast responses stay in the original deferred interaction. A tool-backed or >10-second request keeps its working status visible and replies to that message when channel replies are available; otherwise Maxwell edits the status when possible or sends an interaction follow-up.

Current/latest factual questions in normal chat and `/maxwell` auto mode run `web_search` before generation when available. `/maxwell` also supports `web=search` to force lookup and `web=off` to disable web tools for that turn. Answers cite source URLs; search results are untrusted evidence.

## Operator controls

Owner/admin identity comes from configured admin IDs such as `MAXWELL_OWNER_IDS` / `CREATOR_ID` and persisted admin state.

`/diagnostics` can show redacted runtime/control data. `/maintenance` can make validated persistent updates to `DATA_DIR/bot_control.json`. Both commands independently check configured Maxwell developer IDs. The dashboard uses the same sanitized runtime-control model.

## Runtime controls

Defaults live in `control_defaults.py`; persisted overrides live in `DATA_DIR/bot_control.json`.

Important groups include:

- Replies/triggers and direct-response policy.
- AI/tool concurrency and timeouts.
- Native tool calls and per-tool disable lists.
- Memory/RAG/context budgets.
- Autonomy/REM/background behavior.
- Night/fallback routing.
- Autofix and developer/runtime switches.

`api.state._sanitize_control` is the authority for accepted persisted values/ranges.

## Inbound reliability

Maxwell journals directed inbound request lifecycle state in `DATA_DIR/inbound_requests.sqlite3`. This supports recovery/diagnosis without storing another full copy of private message content.

A request can move through receipt, queue/deferred, running, failure, and delivery states. Maxwell avoids automatic replay after an external side effect may already have happened, because exactly-once delivery cannot be guaranteed across an uncertain crash boundary.

## Docker runtime

The supported deployment is Docker:

- Linux uses `docker-compose.yml` and host networking.
- Docker Desktop uses `docker-compose.bridge.yml` and `host.docker.internal` rewriting for host-local services.
- The Maxwell container supervises bot/API processes together.
- Shell sandboxes and generated-site backends run as sibling containers.

The main service has access to the host Docker daemon. Treat that as a host-root trust boundary even with dropped Linux capabilities and `no-new-privileges`; see [../SECURITY.md](../SECURITY.md).

## Feature flags

Most optional features use `ENABLE_*` switches with `auto|true|false` semantics:

- `auto`: enable when dependencies/config make the feature usable.
- `true`: force it on.
- `false`: keep it off.

Simple installs keep token-spending background loops such as autonomy/REM off by default. `doctor.py` reports resolved feature states.

## Checking an install

Supported Docker install:

```bash
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```

Local development checkout:

```bash
python3 doctor.py
python3 doctor.py --probe
```

Use [INSTALL.md](INSTALL.md) for deployment and [CONFIGURATION.md](CONFIGURATION.md) for settings/runtime controls.
