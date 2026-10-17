# Personal provider keys

Open the private `/config` menu and choose **AI connection**.
Maxwell's default AI works without connecting an account. A saved connection
replaces that model for every message that person sends, including chat,
mentions, `/maxwell`, and message app actions. Other people stay on Maxwell's
model.

1. Choose **Connect API endpoint** and enter its public HTTPS base URL, model ID and API key.
2. Save, then use **Test connection**. This sends a small request and may use provider credit.
3. Open **Advanced** to configure image/audio input, tools, reasoning and effort, output tokens, context size, LLM request timeout and whole-task timeout. Choose only capabilities your model supports. These settings are shared with the dashboard.

There are no hosted-service presets. Older encrypted connections migrate to the generic connection with their exact original destination, key and model. Changing a destination requires entering a key again; leaving it blank may keep a key only at the same endpoint. Keys are never prefilled or displayed in full.

Reasoning levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`; availability depends on the selected endpoint/model. Turning reasoning off clears effort. Request timeouts accept 10–1800 seconds (default 300); whole-task timeouts accept 10–7200 seconds (default 900) and must be at least the request timeout. Context accepts 1024–2000000 tokens; blank keeps the normal prompt budget. Raising a timeout does not raise the step or tool-call budget.

**Use Maxwell's AI** opens a confirmation screen. **Keep connection** cancels;
**Remove key & use Maxwell** deletes the encrypted record and returns app
requests to the default provider. Existing settings and keys are preserved
when upgrading. Unavailable connection controls stay disabled, and an
unreadable saved key can still be removed without decrypting it.

Before enabling this feature, configure a separate 32-byte encryption key as
64 hexadecimal characters in `MAXWELL_BYOK_ENCRYPTION_KEY`. Generate one with
`openssl rand -hex 32`; store it in the deployment secret manager or protected
environment file, never in Git or the SQLite database. If it is missing,
setting a key is disabled and saved credentials cannot be decrypted.

Credentials are stored in `${DATA_DIR}/byok.sqlite3`, encrypted with AES-GCM
and authenticated against the Discord user ID and provider. The SQLite file is
mode `0600`; its containing directory is mode `0700`. Keys are not passed to
prompts, tools, shell containers, or logs. Each request creates a fresh client
for the selected provider, with TLS verification, no redirects, public-address
DNS checks, bounded responses, one retry, the configured request deadline, and at
most eight tool calls. Maxwell-funded fallback providers are not used after a
BYOK failure. Configured endpoints must be public HTTPS OpenAI-compatible
URLs with no login, query, or fragment. Loopback, private, link-local, and
`.local` / `.internal` hosts are rejected. The provider client still checks
DNS and blocks redirects.

The selected provider receives the context Maxwell sends for that request,
which can include the user's prompt, authorized recent context, attachments,
and tool results. Users should choose a provider they trust. Public Discord
history is included only when the user chooses a public reply; private replies
use a separate per-user context.

## Rotate the encryption key

Run rotation during a maintenance window with Maxwell stopped so no process
continues using the old key:

1. Back up the encrypted database and current key using the deployment's secret
   manager.
2. Stop Maxwell and run `python scripts/rotate_byok_key.py --database
   "$DATA_DIR/plugins/maxwell_extras/byok.sqlite3"` with the current key in the environment.
3. Enter and confirm the new 64-character key at the prompts.
4. Replace `MAXWELL_BYOK_ENCRYPTION_KEY` in the secret manager, then restart
   Maxwell and verify a credential status can be read.
5. Remove the old key and temporary backups according to the operator's backup
   retention policy.

If rotation fails before it commits, the old key remains active. If the database
or the new key is lost after commit, saved credentials cannot be recovered;
users must save their provider keys again.
# Provider architecture

Personal compatible API keys use
independent `OpenAICompatibleProvider` clients with a construction-time
`ProviderPolicy`. Modalities on the saved credential control image input,
audio input, native tools, and reasoning. `max_tokens` and `context` set that
client's output cap and prompt budget. Requests do not send a sampling temperature.
They do not inherit Maxwell's funded fallback credentials. Public
HTTPS validation, DNS pinning, TLS verification, redirect blocking, total time
and response-size limits, and credential redaction are enforced by the client.
See [PROVIDERS.md](PROVIDERS.md). ChatGPT login and OAuth are not implemented;
future OAuth credentials must be stored separately from the BYOK API-key table.


## Separate subagent credentials

Use `/config → More options → Subagent AI provider` or select **Subagents
(Premium)** in the dashboard connection section. Save/test/edit/remove works like
the chat connection, but uses `plugins/maxwell_extras/worker_byok.sqlite3` and never
changes the chat slot. Full deletion removes both slots and blocks credential
saves from requests/forms opened before deletion. Workers without their own key
inherit chat BYOK before the operator worker route. Saved unreadable keys stop
execution until repaired or removed. Premium is required to execute workers;
keys can still be reviewed and removed after revocation.

During master-key rotation, with both bot and API stopped, rotate **both**
`$DATA_DIR/plugins/maxwell_extras/byok.sqlite3` and
`$DATA_DIR/plugins/maxwell_extras/worker_byok.sqlite3` (when present) using the
rotation script, the same old key and the same new key. Update the secret and
restart only after every database succeeds; if one fails, keep services stopped
and recover it before switching keys. Keep recovery backups protected. Do not
restart with databases encrypted under different master keys.

Background jobs open a separate connection using the saved worker/chat credential snapshot; they never borrow a chat turn's client. Workers in one job share their eight-tool personal-provider budget. Future chat turns have their own budget and client. Job status/cancel is available through `/tasks` and conversational `manage_tasks`. Reports stay transient even with history off; full deletion removes job metadata and stops old job writes.
