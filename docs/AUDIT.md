# Audit coverage and deployment follow-up

## Scope

This change reviews the core bot and tool dispatcher; providers and external integrations; memory, autonomy, queues and jobs; Discord accounts, threads, plugins and games; the dashboard/API and generated-site stack; configuration, installation and deployment.

The review combines source inspection, existing regression suites, new failing-before/passing-after regressions, and targeted local integration checks. It is not exhaustive execution coverage or a guarantee that the repository is bug-free.

## Repairs

- **Authorization and secrets:** enforce thread visibility and private-memory boundaries, revoke dashboard sessions when admin access is removed, redact nested diagnostics, prevent explicit message destinations from silently falling back to the originating chat, and verify remote mail TLS certificates.
- **Generated-code isolation:** require autofix regressions and run them in constrained, unprivileged, network-disabled Docker containers without host credentials or a Docker socket. Preserve original worktrees as read-only inputs and remove containers after failure, timeout or cancellation.
- **Persistence:** preserve corrupt JSON stores instead of overwriting them, protect shared state updates with file locks, retain valid graph JSON and chess move history, fix scoped-memory deduplication and metadata updates, and guard against stale embedding writes.
- **Lifecycle and concurrency:** repair queued-work shutdown, job/task cleanup, plugin reload and scheduling, Discord REST admission, multipart retry bodies, parallel progress ownership, Telegram delivery receipts/cleanup, and cancelled media subprocesses.
- **Integrations:** correct provider usage attribution, media routing, fallback callbacks and repaired-request retries; respect X posting limits and avoid ambiguous write fallbacks; bound CAPTCHA polling and validate payloads; preserve unrelated DNS TXT records; avoid losing mail after failed fetches.
- **Web/API:** preserve OAuth redirects, separate public stream capacity from admin requests, bound chunked uploads, preserve encoded proxy URLs, restrict probe path traversal, create unique browser profiles, and repair dashboard logout polling and context deletion identifiers.
- **Installation:** restore fresh-install configuration, support the declared Python versions, preserve dotenv values when adapting Docker Desktop addresses, repair health checks and dependency diagnostics, include a Linux Docker CLI and pytest in the runtime image, and use user-relative PM2 log paths.

Regression coverage is in `tests/test_audit_*.py`, with additional updates to existing regression tests. Memory test harnesses now shut down their event loops through `asyncio.run` rather than abandoning background work.

## Local validation

- Full pytest suites on Python 3.11 with core dependencies and Python 3.12 with all optional dependencies.
- Ruff, Python compilation, shell/JavaScript syntax and patch-whitespace checks.
- Caddy configuration validation and Compose configuration checks.
- A real Docker autofix test verified non-root execution, absent host credentials/socket, a read-only source mount and disabled network access.
- A Docker build smoke test verified the bundled Docker CLI runs on the Python runtime base image.

The real provider progress test requires `OLLAMA_BASE_URL` and was not run against a live provider. External Discord, Telegram, X, OAuth, CAPTCHA, Cloudflare and mail calls were mocked. Installing optional voice dependencies allowed the Riva unit test to run, but this does not validate a live voice session. The complete production Docker image and a production rollout were not exercised.

## Deployment actions

1. Rebuild the Maxwell image to obtain the bundled Docker CLI and pytest. Autofix fails closed if its configured local test image is unavailable.
2. Apply the split-origin [Caddy example](../examples/Caddyfile.example): generated pages must not share browser storage with the dashboard. Set `MAXWELL_PUBLIC_BASE_URL` to the generated-site origin and `DISCORD_REDIRECT_BASE`/`DISCORD_REDIRECT_URI` to the dashboard origin. Configure DNS and TLS for both hosts. Clear old-origin credentials and rotate any that may have been exposed.
3. Remote mail servers must present valid certificates. For private certificate authorities, configure `SSL_CERT_FILE`. Loopback and the Docker host gateway retain the existing local self-signed-certificate exception.
4. In Compose, leave `MAXWELL_SITE_DIR` blank for the default or use an absolute host path. Explicit relative values are not supported by the current bind-mount layout.

## Remaining risks and unverified edge cases

- Browser probes still execute generated pages with their page-directed network requests. Initial URL/path validation is not a browser network sandbox. Isolate this service from sensitive internal networks before accepting untrusted pages.
- IMAP UIDVALIDITY changes after mailbox recreation are not tracked. The watermark corrections cover failed fetches and failed persistence, not mailbox replacement.
- Cross-process JSON read-modify-write locking is not uniform across every bot/API writer. The repaired shared store operations are locked, but this is not a complete transaction model for all JSON files.
- Private thread visibility fails closed when membership/cache information is unavailable. Compatibility with live Discord permission/cache behavior still needs operational validation.

These limitations are not claimed as fixed by this change. See [SECURITY.md](../SECURITY.md) for the service's Docker-daemon trust boundary.
