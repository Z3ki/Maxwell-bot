# Maxwell configuration reference

Maxwell has two configuration layers:

1. `.env` for provider endpoints, credentials, identity, paths, feature availability, and startup behavior.
2. Runtime controls in `DATA_DIR/bot_control.json`, editable through the dashboard and owner-only controls.

Keep `.env` and `data/` private.

## Primary AI settings

The easy installer and `.env.simple.example` use provider-neutral names:

| Variable | Purpose |
|---|---|
| `AI_API_URL` | Primary OpenAI-compatible API base URL |
| `AI_MODEL` | Primary chat model |
| `AI_API_KEY` | Primary API key; blank is normal for local endpoints that do not require one |

The simple template maps them to compatibility aliases:

```ini
OLLAMA_BASE_URL=${AI_API_URL}
OLLAMA_MODEL=${AI_MODEL}
OLLAMA_API_KEY=${AI_API_KEY}
```

The full `.env.example` and full installer still use the historical `OLLAMA_*` namespace for the primary provider and its advanced settings. Those names are compatibility names; Maxwell is not Ollama-only.

For an older install:

```bash
python3 scripts/migrate_ai_env.py .env
```

Do not set conflicting values in both namespaces.

## Core environment values

| Variable | Required? | Purpose |
|---|---:|---|
| `DISCORD_BOT_TOKEN` | Yes | Official bot token from the Discord Developer Portal. User/self-bot tokens are not supported. |
| `DISCORD_TOKEN` | Deprecated alias | Used only when the official bot-token setting is empty. |
| `AI_API_URL` / `OLLAMA_BASE_URL` | Yes | Primary OpenAI-compatible endpoint. |
| `AI_MODEL` / `OLLAMA_MODEL` | Yes | Primary chat model. |
| `AI_API_KEY` / `OLLAMA_API_KEY` | Sometimes | Provider key; blank can be valid for local endpoints. |
| `MAXWELL_OWNER_IDS` | Strongly recommended | Comma-separated Discord IDs authorized for owner/admin features. |
| `MAXWELL_ADMIN_USER` | Optional | Dashboard username; defaults to `admin`. |
| `MAXWELL_ADMIN_PASSWORD` | Strongly recommended | Dashboard password. A blank value leaves the admin API unavailable. |
| `MAXWELL_HOST_BIND` | Installer-managed | Absolute host checkout path used by sibling containers. |
| `MAXWELL_SITE_DIR` | Optional | Generated-site directory. Leave unset for the repository default or use an absolute host path. |
| `MAXWELL_PUBLIC_BASE_URL` | Optional | Public origin for generated sites. Keep it separate from the dashboard origin. |

See [`.env.example`](../.env.example) for the complete advanced environment reference.

## Identity and ownership

| Variable | Default | Purpose |
|---|---|---|
| `BOT_NAME` | `Maxwell` | Bot/persona name where a live Discord nickname does not override it. |
| `CREATOR_NAME` | blank | Optional creator label. |
| `CREATOR_ID` | blank | Optional creator Discord ID. |
| `MAXWELL_OWNER_IDS` | blank | Comma-separated owner/admin Discord IDs. |
| `MAXWELL_USER_ID` | blank | Optional configured bot user ID. |
| `COMMAND_PREFIX` | `,` when unset | Prefix for legacy text commands. |
| `BOT_BIRTHDAY` | `2026-05-21` | ISO persona birthday. |
| `BOT_INVITE_URL` | blank | Optional public invite URL. |
| `MAXWELL_USAGE_URL` | blank | Optional provider usage/quota endpoint. |

Blank IDs do not grant implicit ownership.

## `/owner` control panel

The `maxwell_extras` plugin registers an owner-only `/owner` application command. It can show runtime state, controls, memory, autonomy, tools, and plugins; export redacted data; persist validated control changes; and reload the control file.

Changes are sanitized and persisted to `DATA_DIR/bot_control.json`. Secret-like values are redacted and are not editable through the Discord owner panel.

Examples:

```text
/owner action:overview
/owner action:enable key:autonomy_enabled
/owner action:set key:ai_concurrency value:3
/owner action:quota key:123456789012345678
/owner action:quota_set key:123456789012345678 value:200
/owner action:quota_reset key:123456789012345678
/owner action:quota_exempt key:123456789012345678
/owner action:spend key:123456789012345678
```

Customer-facing AI usage is messages, not tokens. The free allowance is 300 messages per rolling five-hour window (`message_quota_limit` / `message_quota_window_seconds`). The ledger lives at `DATA_DIR/message_quota.sqlite3`. One user-visible AI turn, voice utterance, or user-created background job counts as one message. Tool-loop follow-ups do not. `/usage` and `,usage` show that allowance. `/premium` is an optional discovery command and is not a purchase. Premium is not launched: Personal Plus is proposed at $2.99/month per user and Server Plus at $4.99/month per server, using Discord's native Guild Subscription that stays with the purchased server and cannot be transferred. Exact Plus allowances are not decided and are not applied. Billing, checkout, and …

Token and reported API-cost accounting stay internal, in `DATA_DIR/daily_tokens.sqlite3`, as a spending cap. Requests still reserve estimated tokens so concurrent calls cannot oversubscribe that cap. A provider timeout charges the reservation conservatively. Owners inspect that ledger with `/owner action:spend`. Historical Telegram ledger keys remain addressable as `tg:<id>` on the internal ledger only. `quota_clear` removes a user's message-limit override; `quota_unexempt` removes only the exemption.

## Runtime controls

Defaults live in `control_defaults.py`. `bot_control.json` overrides them at runtime.

Frequently used controls include:

| Control | Purpose |
|---|---|
| `bot_enabled` | Master live-response switch |
| `tools_enabled` | Master tool-layer switch |
| `native_tool_calls` | Prefer OpenAI-style provider-native tool calls |
| `disabled_tools` | Per-tool deny list |
| `require_direct_response` | Require an answer/acknowledgement for eligible direct requests |
| `respond_to_edited_mentions` | Allow a newly added direct mention to start one request |
| `ai_concurrency` | Live AI concurrency limit |
| `max_tool_iterations` | Tool-loop iteration cap |
| `tool_iteration_timeout_seconds` | Tool-loop timeout |
| `prompt_context_budget` | Approximate prompt/context budget |
| `memory_context_budget` | Approximate memory contribution budget |
| `live_max_output_tokens` | Maximum output tokens for a live text response (default 4096) |
| `message_quota_limit` | Free messages per rolling window (default 300) |
| `message_quota_window_seconds` | Rolling window length (default 18000, five hours) |
| `message_quota_enabled` | Enforce the customer-facing message allowance |
| `daily_user_token_limit` | Internal per-user token spend cap per UTC day (default 3000000) |
| `daily_user_token_limit_enabled` | Enforce the internal spend cap |
| `premium_billing_enabled` | Forced off. Billing is not available |
| `store_memory` | Conversation-memory storage switch |
| `long_term_memory_enabled` | Long-term memory switch |
| `cross_context_enabled` | Scoped cross-context facts |
| `entity_memory_enabled` | Entity memory |
| `knowledge_graph_enabled` | Relationship/knowledge-graph memory |
| `autonomy_enabled` | Runtime autonomy switch |
| `autofix_enabled` | Runtime autofix switch |
| `enable_night_fallback` | Night-window fallback routing when configured |

The authoritative set and valid ranges are in `control_defaults.py` and `api.state._sanitize_control`. Prefer the dashboard or `/owner` to editing `bot_control.json` while Maxwell is running.

## Message reliability

Directed inbound work is journaled in `DATA_DIR/inbound_requests.sqlite3` with lifecycle state such as received, queued/deferred, running, failed, and delivered. The journal records IDs/state/timing rather than duplicating full private message content.

Relevant controls include `live_turn_timeout_seconds`, `inbound_retry_attempts`, and `inbound_retry_delay_seconds`. Maxwell deliberately avoids blind replay after a tool or Discord send may already have taken effect.

## Memory and RAG

Current Maxwell includes `rag_memory.py`. `RAGMemoryManager` is a vector-backed, SQLite-backed memory manager for channel memory, long-term facts, and scoped shared context using an OpenAI-compatible embedding endpoint.

`knowledge_graph.py` adds entity/relationship memory. REM-style consolidation is optional and controlled separately. Embedding/RAG settings live in the advanced `.env.example`; `doctor.py --probe` checks the configured embedding endpoint when RAG is enabled.

## Optional/background features

Many `ENABLE_*` environment switches accept `auto`, `true`, or `false`. Important examples include `ENABLE_RAG`, `ENABLE_AUTONOMY`, `ENABLE_REM`, and `ENABLE_SHELL`.

Simple installs keep token-spending background loops off by default. The exact dependency detection is implemented in `config.py` and reported by `doctor.py`.

## App-command behavior

For `/maxwell`, fast answers use the original deferred interaction. A tool-backed or >10-second command promotes that interaction to a stable `working on it…` status and sends the final result as a follow-up. Textual `/maxwell` follow-ups are rendered as embeds unless the response already contains an explicit rich payload.

## Tool safety behavior

Tools marked destructive are blocked when the current turn has been tainted by fetched/web content. A fresh user message starts a clean turn. There is no manual confirmation command that overrides a tainted destructive action.

See [../SECURITY.md](../SECURITY.md) for the Docker-daemon trust boundary and deployment warnings.

## Reconfigure

For a simple/easy install, edit the friendly `.env` values and restart:

```bash
cd ~/maxwell
nano .env
./run.sh -d
```

For the full wizard:

```bash
cd ~/maxwell
./install.sh --local --reconfigure
```

Then validate:

```bash
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```
