# Public runtime hardening — development branch review

This change is reviewed against `dev` at `93ba4be`. It is not a production
release or a public stability claim. No stored plugin, game, site, or memory
data is deleted.

Source status: PR #48 was merged into `main` by the repository owner on
2026-09-28. That merge did not deploy or restart the public service. Follow-up
work is being reviewed against `dev`; runtime and data migrations still require
a disposable dev deployment and operator review.

Current generated-site support supersedes this review's `site_server` retirement:
the tool is owner/admin-only and deploys into a mandatory gVisor runtime and
bounded aggregate site pool. The standard stack is FastAPI/Uvicorn + SQLite.
See [CONFIGURATION.md](CONFIGURATION.md#generated-site-custom-backends) and
[SITE_BACKEND_MIGRATION.md](SITE_BACKEND_MIGRATION.md). The execution map below
records the original retirement decision, not the current capability.

## Execution map and decisions

| Boundary | Actual path | Status |
| --- | --- | --- |
| Discord ingress and requests | `bot.py` → context/model loop → `_execute_tool_by_name` → `_invoke_request_tool` | Retained; tool execution now rejects retired coding and host control names even if an old registry offers them. |
| Plugin setup and discovery | `PluginManager.load_plugins()` → plugin `setup()` → `sync_bot_tools()` | Retired workers, repository automation, global plugin administration, and protected prompt-edit plugins are skipped before setup. Shell is retained with gVisor-only execution and fails closed without host policy. Reload drops stale published tools. |
| Chess | `plugins/chess` → `chess_game.py` | `dev` already fixes the reported `__CHESS_IMPORTED__` NameError and tests it through dispatch. |
| Public site creation | `plugins/sites` → static files and optional simple KV API | Retained. `site_server` code execution and Docker lifecycle tool are no longer registered for AI calls. Existing backend containers and their data were not migrated. |
| Discord output | `DISCORD_CHAT_PROTOCOL` → `_send_with_slowmode` | The prompt names native Discord formatting. The normal send/reply path and bot client suppress mentions; user-install webhook replies, `/config`, purge confirmations, progress edits, and legacy proposal interactions now pass explicit no-mention policy. Direct media sends use the bot client default. A dev-bot render check remains outstanding. |
| External providers | `config.py` → `providers.py`, image and speech plugins | Endpoint and model are environment-configurable. The checked-in defaults do not identify the live API account, service tier, or data terms. No provider privacy assurance can be published from this checkout. |
| Website and policies | `web/index.html`, `web/admin/index.html`, `web/privacy/index.html`, `web/terms/index.html`, `api/api_server.py` | Existing admin/API and policy pages remain. The published text still describes retired shell behavior and lacks verified provider retention and review details; replacement policies require operator and legal review. |
| Deployment | `docker-compose.yml`, `docker-compose.dev.yml`, `site_server.py` | Dev Compose is a separate project: own checkout, token, network, named volumes, no host ports, and no Docker socket. Production remains the host-network service that mounts the socket. Socket removal and site-backend migration are still release blockers. |

## Why these tools were retired

`github_projects` was enabled by default and `setup()` registered a 90-second
poller. Its service could run container shell and Git operations and create
background AI jobs. `plugin_workbench` was a model-callable tool that could
propose and apply live plugin code. The former `shell` exposed host-backed
command execution; it is now retained only through the fail-closed gVisor
sandbox. `site_server` and `manage_plugin` exposed Docker-backed code
deployment or global plugin administration. The former personality plugin let an AI tool edit prompt
state for arbitrary server IDs. A 2026-09-25 follow-up removed that plugin,
its command aliases,
the dashboard prompt editor API, and server prompt accessors from the memory
service. It pins the shared persona in the runtime and control sanitizers. The
private `/personality` preference is stored by Discord user ID and only affects
that user's requests.

Code install, removal, and reload return a retired notice. `/config` has
personal, per-server, and protected application-owner scopes. Per-server plugin
selection applies to optional plugin tools and guild-scoped event callbacks;
scheduled jobs remain controlled by the application owner. Personal style is
stored by Discord user ID, while the shared persona remains code-owned.

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

Earlier full-suite runs hung in `test_api_corrupt_writes.py` and
`test_api_rem::test_set_presence_keeps_command_lifecycle_status`; those hangs
did not reproduce in the latest full local Python 3.12 run. The original
Python 3.11/3.12 CI matrix passed the full pytest suite and all three repository
benchmarks. The latest local Python 3.12 full pytest run also passes, with three
optional skips for missing Chromium, live Ollama, and Riva. Focused tests cover
per-server plugin filtering, request cancellation, user-install mention
suppression, and shell lifecycle. The host-level gVisor/firewall integration
script has not run here because this workspace has no Docker or `runsc`. No
real Discord render, provider request, isolated-dev load/resource measurement,
Docker restart recovery, or production measurement was performed. Follow-up CI
on PR #49 passed for Python 3.11 and 3.12, including all three repository
benchmarks. The system Python 3.14 environment
remains incompatible with the declared Discord dependency; use Python
3.11/3.12.

## Release and rollback

Review follow-up changes against `dev`; do not deploy them yet. In a separate
dev Discord application, confirm that static sites, chess, reminders, message
delivery, and mention rendering still work. Back up plugin and site data before
a future site-backend migration. Reverting this commit restores the old tool
catalog; doing so on the public bot would also restore the retired execution
paths, so rollback should be another reviewed change with equivalent guards.
Existing backend containers may continue to run independently under Docker
restart policies and need a separate inventory and migration plan. No host
inventory or migration was performed in this workspace.

## Remaining high-priority work

1. **Done in source:** Telegram transport and generated references are removed; `tests/test_telegram_removed.py` pins that behavior. Stored `tg:<id>` ledger keys remain addressable and no Telegram rows were deleted.
2. **Done in follow-up source:** `/config` controls optional plugin tools and guild-scoped event callbacks per server, with fresh server permission checks. Personal style is per-user; shared prompt editing stays retired. Scheduled plugin jobs remain application-owner controlled.
3. **Code audit updated:** model, tool, user-install, settings, purge, and progress output paths suppress implicit mentions. Validate formatting, chunking, embeds, and replies with the separate dev bot before release.
4. **Release blocker:** production Compose still mounts the Docker socket for legacy site/dashboard lifecycle operations. `scripts/inventory_site_backends.py` and `docs/SITE_BACKEND_MIGRATION.md` prepare a private read-only inventory and migration plan. Run them on the operator host, back up existing site containers/data, migrate those operations behind a narrower service, and only then remove the socket. No production data or containers were inspected or changed here.
5. **Partially verified:** the local full pytest suite and Python 3.11/3.12 follow-up CI pass, including repository benchmarks. Host containment, restart recovery on the isolated deployment, and live resource measurements remain unverified because Docker, `runsc`, and the dev host are unavailable in this workspace.
6. **External review blocker:** provider endpoints and account-level data settings are not present in this checkout. A review draft is in `docs/policy-drafts/`; fill it only after verifying the deployed accounts, provider settings, and operator identity, then obtain operator/legal review before changing the published pages. The current MIT source license remains unchanged. No premium price, quota, billing, or entitlement has been implemented.
