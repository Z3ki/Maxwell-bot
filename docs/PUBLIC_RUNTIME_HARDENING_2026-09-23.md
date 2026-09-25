# Public runtime hardening — development branch review

This change builds on `dev` at `b377643`. It is not a production release or a
public stability claim. No stored plugin, game, site, or memory data is deleted.

## Execution map and decisions

| Boundary | Actual path | Status |
| --- | --- | --- |
| Discord ingress and requests | `bot.py` → context/model loop → `_execute_tool_by_name` → `_invoke_request_tool` | Retained; tool execution now rejects retired coding and host control names even if an old registry offers them. |
| Plugin setup and discovery | `PluginManager.load_plugins()` → plugin `setup()` → `sync_bot_tools()` | Retired workers, repository automation, shell, global plugin administration, and protected prompt edit plugins are skipped before setup. Reload drops stale published tools. |
| Chess | `plugins/chess` → `chess_game.py` | `dev` already fixes the reported `__CHESS_IMPORTED__` NameError and tests it through dispatch. |
| Public site creation | `plugins/sites` → static files and optional simple KV API | Retained. `site_server` code execution and Docker lifecycle tool are no longer registered for AI calls. Existing backend containers and their data were not migrated. |
| Discord output | `DISCORD_CHAT_PROTOCOL` → `_send_with_slowmode` | Prompt now names native Discord formatting. The central normal send/reply path suppresses all mentions; the client also defaults to no mentions. Other interaction and media paths still need a complete send-boundary audit. |
| External providers | `config.py` → `providers.py`, image and speech plugins | Endpoint and model are environment-configurable. The checked-in defaults do not identify the live API account, service tier, or data terms. No provider privacy assurance can be published from this checkout. |
| Website and policies | `web/index.html`, `web/admin/index.html`, `web/privacy/index.html`, `web/terms/index.html`, `api/api_server.py` | Existing admin/API and policy pages remain. The published text still describes retired shell behavior and lacks verified provider retention and review details; replacement policies require operator and legal review. |
| Deployment | `docker-compose.yml`, `docker-compose.dev.yml`, `site_server.py` | Dev Compose is a separate project: own checkout, token, network, named volumes, no host ports, and no Docker socket. Production remains the host-network service that mounts the socket. Socket removal and site-backend migration are still release blockers. |

## Why these tools were retired

`github_projects` was enabled by default and `setup()` registered a 90-second
poller. Its service could run container shell and Git operations and create
background AI jobs. `plugin_workbench` was a model-callable tool that could
propose and apply live plugin code. `shell`, `site_server`, and `manage_plugin`
exposed privileged shell, Docker-backed code deployment, or global plugin
administration. The former personality plugin let an AI tool edit prompt
state for arbitrary server IDs. A 2026-09-25 follow-up removed that plugin,
its command aliases,
the dashboard prompt editor API, and server prompt accessors from the memory
service. It pins the shared persona in the runtime and control sanitizers. The
private `/personality` preference is stored by Discord user ID and only affects
that user's requests.

The slash `/plugins` command can still list and toggle already available
plugins. Code install, removal, and reload now return a retired notice. This
does **not** implement per-guild `/config plugins`; the current enable state
is global or per-user and must be redesigned before broad release.

## Verification and limits

Run with the declared dependencies in Python 3.12:

```text
/tmp/maxwell-py312-20260923/bin/python -m pytest tests/test_chess.py tests/test_chess_runtime.py tests/test_retired_agent_runtime.py tests/test_discord_output_safety.py tests/test_split_response.py tests/test_plugin_system.py tests/test_plugin_architecture.py -q
# 70 passed (Discord/model I/O mocked)
/tmp/maxwell-py312-20260923/bin/ruff check .
# passed
UV_CACHE_DIR=/tmp/maxwell-uv-cache uv pip check --python /tmp/maxwell-py312-20260923/bin/python
# 27 packages compatible
```

The complete 1,939-test collection did not finish: `test_api_corrupt_writes.py`
hangs at its second test when the tests run together, while that test passes
alone. Excluding it reaches another sequence-dependent hang in
`test_api_rem.py::test_set_presence_keeps_command_lifecycle_status`. Both runs
were stopped by timeout; they are not green test results. The system Python
3.14 environment also contains incompatible `discord.py-self` and fails
collection at `discord.ui`, so use the declared official `discord.py` under
Python 3.11/3.12 for CI. No real Discord render, provider request, load test,
Docker restart, or production measurement was performed.

## Release and rollback

Review this branch against `dev`; do not merge to `main` or deploy yet. In a
separate dev Discord application, confirm that static sites, chess, reminders,
and message delivery still work. Back up plugin and site data before a future
site-backend migration. Reverting this commit restores the old tool catalog;
doing so on the public bot would also restore the retired execution paths, so
rollback should be through another reviewed `dev` change with equivalent
guards. Existing backend containers may continue to run independently under
Docker restart policies and need a separate inventory and migration plan.

## Remaining high-priority work

1. Telegram transport and generated references are removed on this branch. Stored `tg:<id>` ledger keys remain addressable; no Telegram database rows are deleted.
2. Replace global/per-user plugin toggles and protected prompt editing with authorized, isolated Discord `/config` and `/personality` commands.
3. Audit all Discord sends, edits, slash responses, chunking, and user-supplied content for formatting and mention safety; validate with a dev bot.
4. Remove or isolate Docker socket access after migrating existing site backends and dashboard operations.
5. Resolve full-suite hangs, test restart recovery and tool side effects, and measure resource use on the isolated dev deployment.
6. Verify the *actual* provider endpoint/account terms, then draft reviewable privacy and service policies. The current MIT source license remains unchanged. No premium price, quota, billing, or entitlement has been implemented.
