# Security Policy

## Reporting

Open a private security advisory or contact the maintainer privately before disclosing vulnerabilities.

## Deployment warnings

- Never expose `/api/*` or `/data/*` without backend authentication.
- Never publish `.env`, `data/`, logs, generated sites, PM2 dumps, API credentials, or authentication material.
- Rotate any credential that has appeared in logs, process environments, shell history, screenshots, chat transcripts, or public artifacts.
- Use only an official Discord **bot token** from the Discord Developer Portal. User/self-bot tokens and copied browser authorization headers are not supported.
- Keep generated sites separate from the authenticated operator API origin. The browser admin dashboard has been removed; delete any stale deployed `admin/` static files. Use [`examples/Caddyfile.example`](examples/Caddyfile.example) to deny `/admin` on both origins and keep operator endpoints off the public origin.
- Treat browser/site probes as capable of making page-directed network requests. Do not place the service on a network where untrusted generated pages can reach sensitive internal services.
- The Maxwell service can control the host Docker daemon through `docker.sock`. A read-only socket mount, dropped capabilities, and `no-new-privileges` do **not** restrict Docker API operations or prevent host-root-equivalent control through that daemon. Treat the Maxwell service as host-root trusted. Shell/site sibling containers have narrower mounts but are still orchestrated through the daemon.
- Public shell commands run only in a tenant-specific container using gVisor `runsc`; root is available inside the guest with a small allow-list of in-guest capabilities needed for package installation and development. Other capabilities are dropped and privilege escalation is disabled. A missing runtime, filtered network, host firewall marker, or bounded storage option disables shell execution. No host filesystem, Docker socket, host namespace, repository, database, or application credential is mounted into the guest.
- Shell traffic uses the operator-provisioned `maxwell-shell-egress` network. Host firewall rules block host-local destinations, private/reserved IPv4 ranges, the sandbox subnet, IPv6, and operator-listed internal ranges before allowing public IPv4 egress. The host filter is required because a Docker bridge alone is not an egress policy.
- Each authenticated user receives separate shell state for each guild and a separate private workspace. Container RAM, CPU, processes, writable layer, workspace size, command time, output, per-user execution, and total concurrency have hard limits. Idle expiry, timeout, cancellation, and service restart remove the user's container; workspace files and installed packages are ephemeral and are lost on reset.
- gVisor is a userspace application kernel, not a hardware VM. It reduces host-kernel exposure but still depends on Docker, the host kernel, the gVisor runtime, and the host firewall. Do not describe a shared-kernel container or gVisor as an absolute isolation boundary. The Maxwell controller retains Docker-daemon access and remains a host-root trust boundary; shell code never receives the Docker socket.
- Public shell isolation and its host setup are documented in [`docs/SHELL_SANDBOX.md`](docs/SHELL_SANDBOX.md). Personal provider-key storage, privacy, and rotation are documented in [`docs/BYOK.md`](docs/BYOK.md).

## Fetched/web content and destructive tools

Fetched pages and web-search results are untrusted input. When the current turn becomes tainted by fetched/web content, tools marked destructive are blocked for that turn. A fresh user message starts a clean turn.

The previous manual-confirmation override flow was removed. There is no confirmation command that converts a tainted destructive action into an allowed one.

This is a defense-in-depth boundary, not a guarantee that web content is safe. Keep powerful tools disabled where they are not needed and limit Discord permissions to the minimum required.

## Operator surfaces

`/diagnostics` and `/maintenance` are authorized against configured Maxwell developer IDs and return ephemeral output. Diagnostic exports redact credential-like values, and secret-like controls cannot be edited through Discord. `/config` checks the caller's server permissions before changing server-scoped settings; personal settings remain private to the caller.

The operator API requires its own Basic authentication. Dashboard Discord OAuth login and browser bearer sessions have been removed. Do not treat Discord owner authorization as a substitute for API authentication or reverse-proxy isolation.

## Secrets and data

`data/` can contain conversation/memory state, request lifecycle metadata, runtime controls, generated-site metadata, and admin state. Back it up and protect it as application data.

The inbound request journal intentionally stores lifecycle metadata rather than a second complete copy of private Discord message content, but IDs/timestamps/state are still operational data and should not be published.
