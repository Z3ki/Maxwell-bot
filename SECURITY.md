# Security Policy

## Reporting

Open a private security advisory or contact the maintainer privately before disclosing vulnerabilities.

## Deployment warnings

- Never expose `/api/*` or `/data/*` without backend authentication.
- Never publish `.env`, `data/`, logs, generated sites, PM2 dumps, dashboard credentials, or authentication material.
- Rotate any credential that has appeared in logs, process environments, shell history, screenshots, chat transcripts, or public artifacts.
- Use only an official Discord **bot token** from the Discord Developer Portal. User/self-bot tokens and copied browser authorization headers are not supported.
- Generated sites must use a separate origin from the admin UI. Arbitrary generated JavaScript sharing the dashboard origin could read browser storage/credentials for that origin. Use the split-origin [`examples/Caddyfile.example`](examples/Caddyfile.example).
- Treat browser/site probes as capable of making page-directed network requests. Do not place the service on a network where untrusted generated pages can reach sensitive internal services.
- Autofix runs generated regression tests only in the configured local Docker test image (`MAXWELL_AUTOFIX_TEST_IMAGE`, default `maxwell:local`) with no network, no host credentials/socket, an unprivileged user, and resource limits. A passing regression is required before it pushes a topic branch. If the isolation image is unavailable, autofix fails closed.
- The Maxwell service can control the host Docker daemon through `docker.sock`. A read-only socket mount, dropped capabilities, and `no-new-privileges` do **not** restrict Docker API operations or prevent host-root-equivalent control through that daemon. Treat the Maxwell service as host-root trusted. Shell/site sibling containers have narrower mounts but are still orchestrated through the daemon.
- The shell sandbox is destroyed after 10 minutes idle (`MAXWELL_SHELL_IDLE_SECONDS`, `0` disables). Recycle wipes `shelldocker/` (`/home/maxwell`) and starts a new container so leftover processes, packages, and workspace files do not accumulate.

## Fetched/web content and destructive tools

Fetched pages and web-search results are untrusted input. When the current turn becomes tainted by fetched/web content, tools marked destructive are blocked for that turn. A fresh user message starts a clean turn.

The previous manual-confirmation override flow was removed. There is no confirmation command that converts a tainted destructive action into an allowed one.

This is a defense-in-depth boundary, not a guarantee that web content is safe. Keep powerful tools disabled where they are not needed and limit Discord permissions to the minimum required.

## Operator surfaces

`/diagnostics` and `/maintenance` are authorized against configured Maxwell developer IDs and return ephemeral output. Diagnostic exports redact credential-like values, and secret-like controls cannot be edited through Discord. `/config` checks the caller's server permissions before changing server-scoped settings; personal settings remain private to the caller.

The browser dashboard/API must still be protected with its own authentication. Do not treat Discord owner authorization as a substitute for dashboard authentication or reverse-proxy isolation.

## Secrets and data

`data/` can contain conversation/memory state, request lifecycle metadata, runtime controls, generated-site metadata, and admin state. Back it up and protect it as application data.

The inbound request journal intentionally stores lifecycle metadata rather than a second complete copy of private Discord message content, but IDs/timestamps/state are still operational data and should not be published.
