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
| Memory storage | `rag_memory.py`, `knowledge_graph.py` | Authorized persistence, retrieval, embedding recovery |
| Scheduling | `jobs.py`, `autonomy.py`, `rem.py` | Host scheduling with feature policy supplied by plugins |

See [MAINTAINABILITY.md](MAINTAINABILITY.md) for module ownership, lifecycle
contracts, migration behavior, and the regression checks.

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
