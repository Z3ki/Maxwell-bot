# Personal provider keys

Open the private `/config` menu and choose **AI connection**.
Maxwell's default AI works without connecting an account. Personal connections
apply to `/maxwell` and message app actions; normal bot mentions continue using
Maxwell's AI.

1. Choose OpenAI, OpenRouter, Groq, or **Other provider**. Selecting a provider
   only changes the setup screen; it does not replace an active connection.
2. Choose **Connect provider** and enter your API key and model name. Named
   providers use their official URL. Other provider also asks for a public
   HTTPS OpenAI-compatible API base URL.
3. Save, then use **Test connection**. The test sends a small request that may
   use provider credit. Results explain whether to check the key, model,
   billing, rate limits or endpoint. OpenAI API billing is separate from
   a ChatGPT subscription; this is an API-key connection, not ChatGPT login.

The screen shows the active provider and model separately from the provider
being configured. Only one personal connection is saved at a time. Saving a
new provider replaces the old connection. **Edit connection** keeps the saved
key when its field is blank, and preserves that provider's URL and advanced
settings. Keys are never prefilled into a form or displayed in full.

**Advanced** has normal selectors for image input, audio input, tools and
reasoning, plus separate forms for reply limits, creativity, reasoning effort,
context token limits and the API base URL. Enable only features the chosen
model supports. Image and audio input do not enable image or voice generation.
Leaving a named provider's URL blank restores its official URL; custom
providers always require one.

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
DNS checks, bounded responses, one retry, a 5-minute request ceiling, and at
most eight tool calls. Maxwell-funded fallback providers are not used after a
BYOK failure. Named providers use their official HTTPS endpoints unless the
user supplies another one. Custom endpoints must be public HTTPS OpenAI-compatible
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
   "$DATA_DIR/byok.sqlite3"` with the current key in the environment.
3. Enter and confirm the new 64-character key at the prompts.
4. Replace `MAXWELL_BYOK_ENCRYPTION_KEY` in the secret manager, then restart
   Maxwell and verify a credential status can be read.
5. Remove the old key and temporary backups according to the operator's backup
   retention policy.

If rotation fails before it commits, the old key remains active. If the database
or the new key is lost after commit, saved credentials cannot be recovered;
users must save their provider keys again.
# Provider architecture

Personal OpenAI, OpenRouter, Groq, and custom OpenAI-compatible API keys use
independent `OpenAICompatibleProvider` clients with a construction-time
`ProviderPolicy`. Modalities on the saved credential control image input,
audio input, native tools, and reasoning. `max_tokens` and `context` set that
client's output cap and prompt budget. They do not inherit Maxwell's funded
fallback credentials. Public
HTTPS validation, DNS pinning, TLS verification, redirect blocking, total time
and response-size limits, and credential redaction are enforced by the client.
See [PROVIDERS.md](PROVIDERS.md). ChatGPT login and OAuth are not implemented;
future OAuth credentials must be stored separately from the BYOK API-key table.
