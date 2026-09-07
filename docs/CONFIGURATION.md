# Maxwell configuration quick reference

The installer writes `.env` from `.env.example` and updates only the keys it asks about. Keep `.env` private; it is ignored by git.

## Values set by the wizard

| Variable | Required? | Purpose |
|---|---:|---|
| `DISCORD_TOKEN` | Yes | Discord **user** token for the self-bot account. Treat it like a password. |
| `OLLAMA_BASE_URL` | Yes | OpenAI-compatible base URL. A bare host such as `http://localhost:11434` gets `/v1` appended by the provider code. |
| `OLLAMA_MODEL` | Yes | Chat model name served by that endpoint. |
| `OLLAMA_API_KEY` | Sometimes | ****** for hosted providers such as OpenRouter or OpenAI; blank is normal for local Ollama/LM Studio. |
| `MAXWELL_OWNER_IDS` | Strongly recommended | Comma-separated Discord user IDs allowed to run admin commands. Blank means admin commands are denied to everyone. |
| `MAXWELL_ADMIN_USER` | Optional | Admin username for dashboard/API auth (defaults to `admin`). |
| `MAXWELL_ADMIN_PASSWORD` | Strongly recommended | Password for the admin API/dashboard. Blank makes the API return 503. |
| `ENABLE_AUTONOMY` | Optional | Timed self-directed background actions; off by default to avoid surprise token spend. |
| `ENABLE_REM` | Optional | Timed memory consolidation (also accepted as `REM_ENABLED`); off by default to avoid surprise token spend. |
| `ENABLE_SHELL` | Optional | Shell tool. The supported Docker install leaves this on; the sandbox is a sibling container. |
| `MAXWELL_HOST_BIND` | Set by installer | Host path of this checkout, used when sibling containers bind-mount files. |
| `MAXWELL_SITE_DIR` | `public/bot` | Where `create_site` writes HTML. If this is an absolute path outside the checkout (typical Caddy root `/var/www/maxwell/bot`), docker compose bind-mounts it. Leave it unset to write under the checkout. |

See [`.env.example`](../.env.example) for the full set of advanced knobs, including embeddings, dashboard host/port, TTS, X/Twitter, email, captcha solving, generated-site paths, and tool-specific limits.

## Autofix (self-heal PRs)

When a tool handler raises a programming error (for example `TypeError` from a
dispatcher clash), Maxwell's own chat model — not a sub-agent — drafts a
minimal patch and a unit test, commits them on `fix/autofix-<error>-<timestamp>`,
and opens a GitHub pull request. It never checks out or pushes `main` and never
auto-merges. Turn it off with the dashboard `autofix_enabled` switch or
`MAXWELL_AUTOFIX=false`. Opening PRs needs `gh` or `GITHUB_TOKEN`.

## Message reliability

These controls live in the dashboard's **Replies & Triggers** and
**Concurrency & Limits** sections and are persisted in `DATA_DIR/control.json`.

| Control | Default | Purpose |
|---|---:|---|
| `require_direct_response` | `true` | An eligible DM, personal mention, or reply to Maxwell requires an answer or acknowledgement rather than discretionary `no_response`. Does not bypass blocked channels, ignored users, sleep, or reply switches. |
| `respond_to_edited_mentions` | `true` | Allow a newly added direct mention to start one reply when the original message was not already answered or pending. Ordinary text edits and embed updates do not start extra replies. |
| `live_turn_timeout_seconds` | `180` | Whole live-turn deadline, including preparation and waiting for an AI slot. Range 1–7200 seconds. Long work should use background jobs rather than occupy a live turn indefinitely. |
| `inbound_retry_attempts` | `2` | Maximum safe processing attempts, including the initial attempt; range 1–5. Never automatically repeat a request after a tool/send may have taken effect. |
| `inbound_retry_delay_seconds` | `5` | Base retry delay, range 1–300 seconds. The periodic recovery worker applies backoff. |
| `gap_recovery_max_messages` | `20` | History **page size**, range 0–100, not the total backlog limit. Zero disables history gap scans; durable pending-request recovery remains separate. |

Personal follow-ups now wait their turn instead of cancelling the same user's
earlier question. `,stop` remains an explicit cancellation. Directed requests that
do not fit the in-memory queue remain deferred on disk; they are not expired
because a slow request took five minutes. Unrelated chatter still coalesces.
Role mentions and `@everyone`/`@here` remain soft signals, not guaranteed requests.
Sleep is an explicit suppression policy, not a promise to reply after waking.

### Diagnose an unanswered ping

Keep the Discord **message ID**, channel ID, and timestamp. Correlate them with
`inbound` lifecycle log entries and `DATA_DIR/inbound_requests.sqlite3`.
The journal stores IDs, state, attempt counts, timestamps, fixed reason labels,
and confirmed response IDs—not message content or credentials. Keep the data
directory private and persistent across deployments.

- **No receipt:** compare Discord history with gateway disconnect/resume logs.
  Check channel access and whether that channel had a recovery cursor. A missing
  receipt alone does not prove a Discord library bug.
- **Suppressed:** inspect the reason (reply switch, channel/user restriction,
  sleep, soft-watch policy, or allowed model silence).
- **Queued/deferred/running:** inspect queue pressure, retries, and stage timing.
  The next DM or ping does not cancel this work.
- **Failed:** inspect the recorded failure reason. An uncertain tool/send outcome
  is deliberately not replayed automatically.
- **Delivered:** use the response ID, when available, to find the actual Discord message. A
  generated answer, typing indicator, or progress placeholder is not proof of
  delivery. A partial multi-message reply is distinguished from a complete send.
  Voice-tool delivery currently records confirmation without a response ID.

LLM traces now include the triggering message ID, allowing model/tool decisions
to be joined to receipt and delivery. Their short ring is not a durable record;
use the journal and retained process logs for longer investigations.
Provider timing logs use `request_id` for the same ID and report the actual
endpoint/model selected on each attempt, including nighttime and error fallbacks.

Recovery re-fetches pending messages from Discord rather than keeping copies of
private content in a second database. Deleted messages or revoked permissions
can therefore prevent recovery and produce an explicit failure. A crash between
an external tool/Discord send and its confirmation cannot provide exactly-once
delivery: such requests require operator reconciliation rather than blindly
repeating a potentially completed action. Completed journal history is bounded,
so this is not a permanent archive of every message.

For regressions, compare deployments **by timestamp**, including the effective
control settings and the primary/fallback model actually used. Night routing
can prefer the fallback from local 22:00–09:00. Record the installed versions
with `python -m pip show discord.py-self aiohttp` using the bot's interpreter;
the lower-bound requirements do not pin those versions. Logs, deployed versions,
and affected message IDs are needed to attribute a production incident.

The August 31–September 7, 2026 source-history review identified `c640af9`
(August 31, 20:15 UTC) as introducing both the directed-queue eviction/expiry and
the early receipt watermark paths corrected here. Nighttime fallback was added
just before that window in `ee61a8c` (August 30). These dates identify plausible
regression points, not proof of which version was deployed or which path caused
a particular missed ping.

## Identity

Names and IDs are env-driven. Empty Discord IDs mean no baked-in owner — admin access comes only from `MAXWELL_OWNER_IDS` / `admins.json`.

| Variable | Default | Purpose |
|---|---|---|
| `BOT_NAME` | `Maxwell` | Spoken name in prompts; live Discord nick still wins in chat. |
| `CREATOR_NAME` | empty | Optional human owner label in prompts. |
| `CREATOR_ID` | empty | Optional creator Discord user ID. Blank = not assumed. |
| `MAXWELL_USER_ID` | empty | Optional Discord ID of this bot account. |
| `COMMAND_PREFIX` | `,` | Prefix for this bot's text commands. |
| `BOT_BIRTHDAY` | `2026-05-21` | ISO date used when the bot talks about its birthday. |
| `BOT_INVITE_URL` | empty | Official invite the bot may share. |
| `MAXWELL_USAGE_URL` | empty | Provider quota endpoint for the `usage` tool. |

## Common provider snippets

```ini
# Local Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_API_KEY=
```

```ini
# OpenRouter
OLLAMA_BASE_URL=https://openrouter.ai/api/v1
OLLAMA_MODEL=moonshotai/kimi-k2.6:free
OLLAMA_API_KEY=your-openrouter-key
```

```ini
# OpenAI
OLLAMA_BASE_URL=https://api.openai.com/v1
OLLAMA_MODEL=gpt-4.1-mini
OLLAMA_API_KEY=your-openai-key
```

```ini
# LM Studio
OLLAMA_BASE_URL=http://localhost:1234/v1
OLLAMA_MODEL=the-loaded-model-name
OLLAMA_API_KEY=
```

## Reconfigure

From a cloned checkout:

```bash
./install.sh --local --reconfigure
```

Or, for an existing install made by the one-liner:

```bash
cd ~/maxwell
./install.sh --local --reconfigure
```
