# Maxwell plugin development

Maxwell loads features from `plugins/<name>/`. Adding a tool, prompt slice,
scheduled job, or hook does **not** require editing `bot.py`, `tool_schemas.py`,
or `control_defaults.py`.

Python plugins run **in-process**. A manifest permission is an operator
declaration and a runtime check, not a sandbox. Do not install untrusted
Python from the network without reviewing it. Owner-only Discord and dashboard
controls are the only supported install/enable path.

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
4. Enable it (default comes from `enabled_by_default`, or `,plugin enable`).
5. Use the capability. Some host changes still need a process restart
   (`requires_restart` in the manifest).

## Manifest

`plugin.json` is spec version 1. Useful fields:

| Field | Purpose |
|---|---|
| `id` / `name` | Unique identifier |
| `version` | Plugin version |
| `api_version` | Must be `1` |
| `description`, `author` | Dashboard / `,plugin list` |
| `dependencies` | Other plugin ids that must load first |
| `optional_dependencies` | Python packages; missing ones disable *this* plugin, not Maxwell |
| `permissions` | Declared capabilities (`network`, `shell`, …) |
| `tools` | Tool names + contracts (`returns_result`, `ends_turn`, `is_destructive`) |
| `prompt` | Optional prompt slice, included only when its tools are on the turn |
| `enabled_by_default` | Used when `data/plugins.json` has no entry yet |
| `protected` | Cannot be disabled globally |
| `config_schema` / `default_config` | Plugin-owned settings |
| `requires_restart` | Dashboard warning |

Legacy manifests (`name`, `enabled_globally`, `allowed_users`, `denied_users`) still load.

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
compatibility parser, the dashboard disable list, autonomy, and audit logs.

## Managing plugins

- Discord: `,plugin list|enable|disable|reload|install|uninstall`
- Tool: `manage_plugin`
- Dashboard: Plugins tab
- API: `GET /api/plugins`, `POST /api/plugins/install`,
  `POST /api/plugins/{name}/enable|disable|uninstall`,
  `POST /api/plugins/reload`, `PUT /api/plugins/{name}/config`

Install copies a local directory or zip into `data/installed_plugins/` after
validating `plugin.json`. Uninstall moves the code aside and keeps
`data/plugins/<id>/`. Only independently installed plugins can be uninstalled;
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
