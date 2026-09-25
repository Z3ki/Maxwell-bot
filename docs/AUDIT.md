# Audit coverage and deployment follow-up

> **Status:** this is a dated audit record, not a live test dashboard. It summarizes the major September 2026 reliability/security audit and the follow-up work that landed around it. Exact historical pass counts, source line numbers, and deployment assumptions should not be treated as current. For live setup behavior use [INSTALL.md](INSTALL.md), [CONFIGURATION.md](CONFIGURATION.md), and [OVERVIEW.md](OVERVIEW.md).

## Current post-audit state

Since the original audit snapshot, Maxwell has continued to change. Current `main` now includes, among other things:

- The friendly easy-install provider names `AI_API_URL`, `AI_MODEL`, and `AI_API_KEY`, with `OLLAMA_*` retained as compatibility/advanced names.
- Developer-only `/diagnostics` and `/maintenance` support with redacted data export and validated persisted edits.
- Discord `/maxwell` rich embed output plus slow/tool-backed interaction progress that promotes to a stable `working on it…` state after about 10 seconds and sends the final response separately.
- Provider-native OpenAI-style `tool_calls` as the preferred tool path when supported.
- Fail-closed destructive-tool handling for turns tainted by fetched/web content; the old manual-confirmation override flow was removed.
- A single supported Cloudflare DNS helper entry point that requires an explicit domain/zone rather than personal hard-coded defaults.

The current implementation and tests are authoritative for those features.

## September 2026 audit scope

The audit covered the core bot and tool dispatcher; provider integrations; memory/RAG, autonomy and jobs; Discord accounts/threads/plugins; dashboard/API; generated-site runtime; configuration; installation; and deployment boundaries.

The review combined source inspection, regression tests, failing-before/passing-after tests, and targeted local integration checks. It was not a claim that every external integration or production deployment path had been exercised.

## Repairs covered by the audit

### Authorization and secrets

- Strengthened thread/private-memory boundaries.
- Revoked dashboard sessions when admin access is removed.
- Redacted nested diagnostics and sensitive usage fields.
- Prevented explicit message destinations from silently falling back to the originating chat.
- Required certificate validation for remote mail transport while retaining explicit local exceptions.

### Generated-code isolation

Autofix-generated regression tests were moved into a constrained local Docker test image with no network, no host credentials, no Docker socket, an unprivileged user, and resource limits. Autofix is expected to fail closed if the configured isolation image is unavailable.

### Persistence and shared state

- Corrupt JSON stores are preserved/backed up instead of silently overwritten in audited paths.
- Shared writes use locking/atomic helpers where repaired.
- Graph/chess/memory state received targeted durability fixes.
- Scoped-memory deduplication/metadata and embedding-write races received regressions.

This does not mean every multi-process state path is a full transaction system; see remaining risks below.

### Lifecycle and concurrency

The audit repaired multiple queue/shutdown/task-cleanup problems, plugin reload/scheduling edge cases, Discord REST admission, multipart retries, progress ownership, Telegram receipt/cleanup paths, and cancelled media subprocesses.

The supported container now supervises bot/API processes together and forwards shutdown through the container lifecycle.

### Integrations

Targeted fixes covered provider usage attribution, media routing, fallback callbacks, repaired-request retries, X posting bounds, CAPTCHA polling, DNS TXT preservation, and mail-fetch persistence behavior.

### Web/API

Targeted repairs covered OAuth redirects, stream/admin capacity separation, chunked upload bounds, proxy URL preservation, probe path validation, browser profile separation, dashboard logout polling, and context-deletion identifiers.

### Installation/deployment

The audit repaired fresh-install configuration, Python/dependency compatibility, Docker Desktop address rewriting, health/dependency checks, Docker CLI availability inside the runtime image, and user-relative legacy PM2 paths.

Docker is now the supported runtime; old host/PM2 material is migration/compatibility surface rather than the recommended deployment.

## Deployment requirements that remain important

1. Rebuild the Maxwell image after runtime/dependency/entrypoint changes.
2. Keep generated pages on a separate origin from the dashboard/admin UI. Use [`../examples/Caddyfile.example`](../examples/Caddyfile.example) as the reference split-origin configuration.
3. Set `MAXWELL_PUBLIC_BASE_URL` to the generated-site origin and the Discord OAuth redirect/base to the dashboard origin.
4. Protect `.env`, `data/`, logs, credentials, and dashboard state.
5. Treat the Maxwell container as host-root trusted because it can control the host Docker daemon through `docker.sock`.
6. When `MAXWELL_SITE_DIR` is outside the checkout, use an absolute host path so the Compose bind mount is unambiguous.

## Remaining risks and unverified edge cases

These are deployment/architecture caveats, not claims that an exploit or outage is currently present:

- Browser probes execute generated pages and may follow page-directed network requests. Initial URL/path validation is not a browser network sandbox.
- Mailbox replacement/UIDVALIDITY changes remain a specialized recovery case beyond ordinary fetch/persistence retries.
- Not every cross-process JSON read-modify-write path is a database transaction. Audited shared stores have targeted locking/atomicity fixes, but the repository is not uniformly transactional.
- Private-thread visibility intentionally fails closed when membership/cache information cannot establish access; live Discord cache/permission behavior still deserves operational validation.
- External actions such as Discord sends cannot guarantee exactly-once effects across a crash that occurs after the external side effect but before local confirmation.
- OpenAI-compatible providers differ in streaming/tool-call behavior; mocked unit tests cannot substitute for testing the actual configured endpoint/model.

See [../SECURITY.md](../SECURITY.md) for security boundaries.

## Validation guidance

Do not reuse the old hard-coded historical pass count as proof about the current commit. For the current checkout, run the checks that CI/runtime actually use:

```bash
python -m pip check
ruff check .
python -m pytest
```

For an installed Docker instance:

```bash
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
docker compose logs -f maxwell
```

External Discord, provider, voice, browser, DNS, mail, OAuth, X, Telegram, and other integrations require the corresponding real service/configuration to be considered live-validated.
