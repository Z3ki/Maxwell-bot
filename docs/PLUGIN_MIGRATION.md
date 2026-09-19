# Plugin migration status

This branch migrates Maxwell from a hardcoded tool list in `bot.py` to a
plugin host. Remaining work for a later session:

## Done

- `maxwell_core`: manifests, plugin lifecycle, tool registry, hooks, prompts,
  services, capabilities, provider/memory/transport protocols
- Built-in tools moved out of `bot.py` `_setup_tools` into `plugins/*`
- `bot_tools.py` is a compatibility re-export; implementations live in
  `plugins/*/impl.py` and helpers in `tooling/helpers.py`
- Prompt components from plugin manifests; personality no longer injects age
  unless `BOT_BIRTHDAY` is set; "real person" identity wording removed
- Taint gate fails closed on a fresh user message (no `,confirm`)
- Plugin runtime (`spawn`/`after`/timeouts/health) is core, not extras
- Dashboard Plugins tab + owner API enable/disable/reload/config
- Sample plugin under `examples/sample_plugin/`
- Autonomy routing, visible-output suppression, modern user-install send,
  `/owner` and `/maxwell` embed wrapping use host APIs instead of wrapping
  MaxwellBot methods
- Chat providers (primary / autonomy / aux) are constructed through
  `maxwell_core.providers.factory`
- Memory is published on the service container as `memory`
- Owner plugin install/uninstall from a local directory or zip, with
  rollback of a failed replace and preserved plugin data directories

## Remaining (intentionally later)

- Split `plugins/*/impl.py` further (helpers still share one large module).
- Remote URL plugin install still requires a local path/zip the owner already
  trusts. Workbench covers generated local code. Manifests should set
  `requires_restart` when binary deps change.
- `rag_memory.py` remains the SQLite implementation; plugins should take it
  from `ctx.service("memory")` rather than constructing their own.

Do not re-audit the old `bot.py` `_setup_tools` list — it is gone. Start from
`plugins/*/plugin.json` and `maxwell_core`.
