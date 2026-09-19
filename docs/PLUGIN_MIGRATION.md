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

## Remaining

- Several `maxwell_extras` behaviours still wrap host methods
  (interaction progress, embed output, owner control, visible output guard,
  rich interactions, image-generator capture, autonomy routing). Convert
  those to the new hooks.
- `providers.py` is still the live client; `maxwell_core.providers.factory`
  wraps it. Autonomy/aux/vision slots should go through the factory only.
- `rag_memory.py` should be consumed only via `MemoryService`.
- Split `plugins/*/impl.py` further (helpers still share one large module).
- Third-party install/update/uninstall from a URL, with owner approval and
  rollback, is not implemented (workbench covers human-approved local code).
- Hot-reload of arbitrary binary dependencies still requires a restart;
  manifests should set `requires_restart` when that is true.

Do not re-audit the old `bot.py` `_setup_tools` list — it is gone. Start from
`plugins/*/plugin.json` and `maxwell_core`.
