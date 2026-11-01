# Maxwell plugin development

Maxwell loads features from `plugins/<name>/`. Adding a tool, prompt slice,
scheduled job, or hook does **not** require editing `bot.py`, `tool_schemas.py`,
or `control_defaults.py`.

Python plugins run **in-process**. A manifest permission is an operator
declaration and a runtime check, not a sandbox. Do not install untrusted
Python from the network without reviewing it. Installation and global controls
are operator-only; users can change their own plugin availability with
`manage_plugin`, and server managers can restrict optional plugins for a server.

## Layout

```text
plugins/
    web/
        plugin.json
        __init__.py
        impl.py
```

A new plugin:

1. Create `plugins/<id>/` (`id` must be a Python identifier).
2. Write `plugin.json` (validated before the Python is imported).
3. Implement `setup(bot, ctx)` returning tools and/or calling `ctx.*`.
4. Reload the plugin registry and enable it if needed (the initial global state
   comes from `enabled_by_default`; an operator can use the API).
5. Use the capability. Some host changes still need a process restart
   (`requires_restart` in the manifest).

## Manifest

`plugin.json` is spec version 1. Useful fields:

| Field | Purpose |
|---|---|
| `id` | Unique Python-identifier plugin ID; falls back to `name` or directory name |
| `display_name` / `title` | Display name; defaults to the plugin ID |
| `version` | Plugin version |
| `api_version` | Must be `1` |
| `manifest_version` | Must be `1` when specified |
| `entry` | Relative Python entry file inside the plugin; defaults to `__init__.py`, then `tools.py` |
| `description`, `author` | Plugin metadata shown by Discord controls and the operator API |
| `dependencies` | Other plugin ids that must load first |
| `optional_dependencies` | Python packages; missing ones disable *this* plugin, not Maxwell |
| `required_features` | Config flags that must be on before importing this plugin; missing/off flags omit it |
| `permissions` | Declared capabilities (`network`, `shell`, …) |
| `tools` | Tool names + contracts (`returns_result`, `ends_turn`, `is_destructive`) |
| `prompt` | Optional prompt slice; defaults to requiring any declared tool, but `requires_tools` can override that (an empty list imposes no tool requirement) |
| `enabled_by_default` | Used when `data/plugins.json` has no entry yet |
| `protected` | Cannot be disabled globally |
| `config_schema` / `default_config` | Plugin-owned settings |
| `requires_restart` | Restart requirement reported by the operator API |

Legacy manifests (`name`, `enabled_globally`, `allowed_users`, `denied_users`) still load.

Bundled tool-only plugins share `maxwell_core.plugins.bundled.build_tools`.
Its declarations contain a runtime name, implementation class name, and
optional config flag (or tuple of flags). Disabled tools are omitted and a
fully disabled plugin never imports its implementation. This also keeps
disabled integrations out of the model's tool catalog and prompt slices.

## PluginContext

`setup(bot, ctx)` is the supported entry. `setup(bot)` still works.

```python
def setup(bot, ctx):
    ctx.on_event("on_message", on_message)
    ctx.every(30, tick)
    ctx.register_hook("after_tool", after_tool, priority=50)
    ctx.register_prompt(PromptComponent(...))
    ctx.register_service("my_cache", {})
    ctx.spawn(work(), name="my-bg")
    return [MyTool(bot)]
```

Hooks: `before_message`, `after_message`, `before_prompt`, `after_prompt`,
`before_model`, `after_model`, `before_tool`, `after_tool`, `before_response`,
`after_response`, `interaction`, `format_output`, `progress`, `memory_event`,
`task_lifecycle`, `plugin_lifecycle`, `personality`, `turn_sent`, `channel_send`.

Security gates (taint, Discord permissions, admin) run in the host **before**
`before_tool`. Hooks cannot forge `_confirmed`.

## Tools

Subclass `tools.Tool` and set:

- `tool_name`, `get_description()`, `get_parameters()`
- `returns_result` / `ends_turn` / `is_destructive`
- `requires_admin`, `required_capabilities`, `transports`

The live instance is the source of truth for native tool-calling, the
compatibility parser, the runtime tool disable list, autonomy, and audit logs.

## Managing plugins

- Discord: server managers can select optional tools in `/config`'s server plugin settings; code install, uninstall, and reload are not available there.
- Tool: `manage_plugin` lists/statuses plugins and enables or disables them for the caller; admins may target another user or modify global status.
- API: `GET /api/plugins`, `POST /api/plugins/install`,
  `POST /api/plugins/{name}/enable|disable|uninstall`,
  `POST /api/plugins/reload`, `PUT /api/plugins/{name}/config`

Install copies a local directory or zip into `data/installed_plugins/` after
validating `plugin.json`; reload plugins to activate the installed code.
Uninstall moves the code aside and keeps `data/plugins/<id>/`. Only independently installed plugins can be uninstalled;
bundled plugins are disabled instead.

Disabling a plugin removes its tools, events, prompts, jobs, and hooks from
new requests. It does **not** delete `data/plugins/<name>/`.

## Human-approved plugin development

The `plugin_workbench` tool (maxwell_extras) can scaffold, test, and propose
plugin updates. Applying generated Python still requires an explicit owner
approval. Maxwell must not silently install generated code with host access.

## Sample

See [`examples/sample_plugin/`](../examples/sample_plugin/). Copy it into
`plugins/sample_echo/` — no core edits required.
