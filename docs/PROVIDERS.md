# AI providers and migration

Maxwell's Discord processing uses `ChatProvider` and request-local
`ProviderResult` objects. `OpenAICompatibleProvider` implements the
`/v1/chat/completions` protocol, including streaming, tools, media, reasoning
options, usage, model overrides, cancellation and bounded recovery. Ollama,
OpenAI, OpenRouter, Groq and LM Studio remain supported compatible endpoints.

## Configuration

Both templates and installers prefer `AI_*`. Existing `.env` files work without
editing. `AI_BASE_URL` takes precedence over the older `AI_API_URL` and then
`OLLAMA_BASE_URL` (the first non-empty endpoint wins). `AI_MODEL` similarly
uses the first non-empty model. For credentials, optional routes and generation
controls, a present `AI_*` value wins even when blank; blank keys clear stale
credentials, and blank optional routes disable them. Invalid numeric values
retain the existing default/clamping behavior.

| Preferred name | Deprecated input |
| --- | --- |
| `AI_BASE_URL` | `AI_API_URL`, `OLLAMA_BASE_URL` |
| `AI_API_KEY` | `OLLAMA_API_KEY`, then `OPENAI_COMPAT_API_KEY` |
| `AI_MODEL` | `OLLAMA_MODEL` |
| `AI_MAX_OUTPUT_TOKENS` | `OLLAMA_MAX_TOKENS` |
| `AI_TEMPERATURE` | `OLLAMA_TEMPERATURE` |
| `AI_DISABLE_REASONING` | `OLLAMA_DISABLE_REASONING` |
| `AI_REASONING_EFFORT` | `OLLAMA_REASONING_EFFORT` |
| `AI_RETRY_ATTEMPTS` | `OLLAMA_RETRY_ATTEMPTS` |
| `AI_EMPTY_RESPONSE_RETRIES` | `OLLAMA_EMPTY_RESPONSE_RETRIES` |
| `AI_ENDPOINT_COOLDOWN_SECONDS` | `OLLAMA_ENDPOINT_COOLDOWN_SECONDS` |
| `AI_FALLBACK_*` | Matching `OLLAMA_FALLBACK_*` |
| `AI_VISION_*` | Matching `OLLAMA_VISION_*` |
| `AI_OPENCODE_SESSION` | `OLLAMA_OPENCODE_SESSION`; `OPENCODE_SESSION` still supported |

Fallback suffixes are `BASE_URL`, `MODEL`, `API_KEY`, `DISABLE_REASONING`, and
`REASONING_EFFORT`. Vision suffixes are `BASE_URL`, `MODEL`, `API_KEY`, and
`DISABLE_REASONING`. Blank vision base/key inherit the primary configuration.

Optional migration:

```bash
python3 scripts/migrate_ai_env.py .env
```

The migration keeps deprecated interpolation aliases for older tooling and is
idempotent. Edit the new names afterwards. Deprecated `Config.OLLAMA_*`
attributes remain snapshots for external Python integrations; internal Maxwell
code reads `Config.AI_*`. `OllamaProvider = OpenAICompatibleProvider` remains a
deprecated import alias. No removal date is imposed on existing deployments.

## Prompt caching diagnostics

The leading system instructions and history precede volatile turn context.
Guild emojis/stickers and current reaction snapshots live in that later block;
short messages use the configured history window instead of a smaller cap.
Ordinary history eviction removes message chunks, and tool catalogs/schemas are
sorted independently of registration order. Explicit personal-app history
limits remain exact. Edits, deletions and changes in authorized tools still
change the prompt; correctness and permissions take precedence over cache reuse.
Large per-turn context can also reduce history to fit the model budget.

`/debug` and the debug tool show provider-reported cached input tokens and their
share of input, plus cache writes when supplied. Request-local `usage` and
`timing` retain `cached_tokens` and `cache_write_tokens`. Compatible nested
`prompt_tokens_details` / `input_tokens_details` and DeepSeek's
`prompt_cache_hit_tokens` are accepted. Missing or inconsistent cache metrics
remain unknown; a reported zero is a measured miss. Cache counts are subsets
of input and are not added again to total usage.

Stable prefixes improve reuse opportunities but do not guarantee cache hits.
Provider/model support, routing, retention and any required cache breakpoints
still apply. This Chat Completions client does not inject vendor-specific
cache settings or implement the Responses API. See the official
[OpenAI caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
and [DeepSeek caching guide](https://api-docs.deepseek.com/guides/kv_cache).

## Architecture and extension points

`maxwell_core/providers/models.py` defines immutable `ProviderConfig`,
`GenerationDefaults`, `ProviderCapabilities`, `ProviderPolicy` and authentication
contracts. Credentials are excluded from configuration representations.
The abstract `ChatProvider` has no API-key requirement or OpenAI wire-format
requirement. The factory maps protocol kind `openai_compat` and legacy/vendor
aliases (`ollama`, `openai`, `openrouter`, `groq`, `lmstudio`, `custom`) explicitly
to the compatible implementation. Unknown kinds fail with a configuration error.

`ProviderRouter` owns independent primary, fallback and vision clients. Clients
own their sessions, credential sources and learned model constraints. The router
preserves primary retries before fallback, fast/night fallback preferences,
rate-limit cooldowns, media routing and final text-only degradation, and bounded
non-streaming recovery for empty responses. Retry options belong to each request
so concurrent requests never change a client's generation defaults. Model
overrides apply to the primary slot; alternate providers use their own models.
Personal BYOK clients bypass the funded router entirely.

The concrete HTTP client remains in `providers.py`, alongside actively used
stream parsing and model compatibility helpers. Its old multi-endpoint
constructor keywords remain a deprecated compatibility path for external
integrations; new factory/runtime code constructs independent clients instead.
Do not use those keywords in new code. They can be removed when integrations
have migrated without changing the Discord request pipeline.

`ProviderResult` remains string-compatible to preserve existing text consumers,
and carries `content`, `tool_calls`, `usage`, `assistant_message`, `model`,
`provider`, and `timing`. Metadata is copied per result. Legacy completion-message
mappings also carry request-local usage/timing outside their wire keys. Deprecated
`_last_*` snapshots are diagnostics only. Tool dispatch reads the returned
result exclusively; strings or unrecognized results cannot consume old calls.

`errors.py` defines safe authentication, invalid-request, media-unsupported,
rate-limit, quota, unavailable and empty-response failures. Provider-specific
response interpretation stays in the protocol client, while routing handles
exception types. Task cancellation propagates without retries.

Authentication is injected through an async `headers()` contract at request
time. `BearerAuthentication` handles static or personal API keys; custom
implementations can resolve a fresh access credential without changing
`ChatProvider`. No OAuth login, refresh, account storage or Responses API is
implemented. A future native/Responses provider implements `ChatProvider`,
returns `ProviderResult`, classifies failures using the shared exceptions, and
is registered through the factory or plugin service container.

## BYOK policy

BYOK construction supplies an immutable policy before session creation:
public HTTPS only, DNS results checked and pinned, private IP literals rejected,
verified TLS, redirects blocked, provider fallback forbidden, 300-second total
request deadline, 2 MiB response limit, and 4096 output-token limit. Each user
request owns a separate client and encrypted API-key record. Sensitive requests
remain non-streaming so credential echoes are scrubbed before progress UI or
the final result receives content. Upstream errors and authentication failures
are sanitized. OAuth credentials must use a separate future storage subsystem.

## Retained compatibility behavior

OpenCode requires `x-opencode-session` on its compatible chat endpoint; this is
a small host-specific header adapter, not a provider kind. Older compatible
servers reject streaming usage options, tool definitions, certain temperatures
or output limits. These workarounds remain covered by provider tests. DeepSeek
answer-in-reasoning handling stays restricted to known models to avoid exposing
other models' internal reasoning. Audio/video adaptation and empty streamed
response recovery also remain supported.

Remaining `OLLAMA_*` references are deprecated environment/import migration
paths, compatibility tests, historical release/audit notes, or genuine Ollama
service controls (`OLLAMA_ORIGINS` and the old embedding URL alias).
