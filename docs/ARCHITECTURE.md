# Maxwell architecture (plugin host)

Maxwell combines a Discord host, shared services, and feature plugins. The host
still coordinates the turn loop; protocol parsing, prompt construction, output
cleanup, memory policy, and plugin lifecycle have explicit module boundaries.

| Layer | Implementation | Role |
|---|---|---|
| Discord host | `bot.py` | Composition, gateway and app requests, turn coordination |
| Request lifecycle | `message_pipeline.py`, `concurrency_safety.py` | Admission, per-channel ordering, interruption, cleanup |
| Shared services | `maxwell_core/` | Plugins, registries, prompts, transport helpers, provider protocols, memory policy |
| Features | `plugins/<feature>/` | Tools, prompts, jobs, hooks, API extras |
| Provider adapter | `providers.py` | OpenAI-compatible requests, endpoint fallback, retry policy |
| Memory storage | `conversation_memory.py` | Scoped SQLite transcript history without vector or local inference work |
| Scheduling | `jobs.py`, `autonomy.py`, `rem.py` | Host scheduling with feature policy supplied by plugins |

See [MAINTAINABILITY.md](MAINTAINABILITY.md) for module ownership, lifecycle
contracts, migration behavior, and the regression checks.

Transcript database work runs on one FIFO worker, not the Discord event loop.
Whole writes and retention deletes stay in one transaction; cancellation does
not interrupt an accepted transaction, and shutdown drains accepted work.
Cleanup uses a covering partial index and atomically removes transcripts from
channels whose newest stored user message is older than 12 hours. Bot output
does not extend that existing cutoff. Archived facts and Discord messages are
not deleted by this sweep.

Older transcript recall uses a contentless SQLite FTS5 index maintained by
insert/edit/delete triggers. Queries require exact requester channel/guild
scope, exclude recent history, and run on the same worker. Up to four bounded
matches follow the stable transcript prefix as user-role reference material;
optional recall is trimmed before recent history. No embedding provider or
archived fact/vector retrieval participates in this path.

The request journal has its own FIFO worker and WAL database with FULL
synchronous durability. Receipt acceptance and irreversible-effect checkpoints
are awaited before advancing a watermark or calling Discord/tools. Synchronous
queue callbacks enqueue transitions immediately; later journal operations see
them in FIFO order. Turn ownership starts before journal awaits so same-user
interrupts also cancel and persist requests still waiting to begin. Shutdown
drains the journal worker after disconnecting Discord.

## What stays in core

- Process init/shutdown
- Plugin discovery, manifests, lifecycle, dependency order
- Tool registry and dispatch (native + compatibility)
- Prompt composition
- Hook bus
- Permission / taint / owner checks
- Config and secrets
- Shared storage helpers
- Scheduling infrastructure (`ctx.every`, `ctx.spawn`)
- Observability (plugin health)

The core does not contain a hardcoded list of chess, web search, or image
tools. Those live in `plugins/`.

## What is a plugin

Anything that can be added without editing the host:

- AI tools
- Discord / app / context-menu commands (via extras + hooks)
- Event handlers and message processors
- Prompt components
- Providers and memory backends
- Background jobs and autonomous behaviours
- Authenticated `/api/plugin/<id>/…` routes
- Storage namespaces under `data/plugins/<id>/`

## Compatibility

`plugin_manager.py` and `bot_tools.py` remain import paths:

- `plugin_manager.py` re-exports the core manager
- `bot_tools.py` re-exports helpers + plugin tool classes so existing tests
  and `from bot_tools import WebSearchTool` keep working

New code should import from `maxwell_core` and `plugins.<id>`.
Existing helper imports from `bot.py` and `providers.py` remain available while
their implementations live in the shared service modules.

## Remaining host work

See [PLUGIN_MIGRATION.md](PLUGIN_MIGRATION.md) for leftover extras
monkey-patches, provider slots, and memory protocol adoption.
