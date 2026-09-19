# Maxwell architecture (plugin host)

Maxwell is a small core plus feature plugins.

```text
Discord / Telegram transport
        │
        ▼
     bot.py  (composition root, Discord client, turn loop)
        │
        ▼
 maxwell_core
   plugin manager · tool registry · prompt manager · hook bus · services
        │
        ▼
 plugins/<feature>/   (tools, prompts, jobs, hooks, dashboard/API extras)
        │
        ├── providers.py   OpenAI-compatible adapter (pluggable factory)
        ├── rag_memory.py  MemoryService implementation
        └── jobs / autonomy / rem   host schedulers, feature policy in plugins
```

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
- Dashboard panels and `/api/plugin/<id>/…` routes
- Storage namespaces under `data/plugins/<id>/`

## Compatibility

`plugin_manager.py` and `bot_tools.py` remain import paths:

- `plugin_manager.py` re-exports the core manager
- `bot_tools.py` re-exports helpers + plugin tool classes so existing tests
  and `from bot_tools import WebSearchTool` keep working

New code should import from `maxwell_core` and `plugins.<id>`.

## Remaining host work

See [PLUGIN_MIGRATION.md](PLUGIN_MIGRATION.md) for leftover extras
monkey-patches, provider slots, and memory protocol adoption.
