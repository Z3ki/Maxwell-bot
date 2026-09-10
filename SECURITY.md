# Security Policy

## Reporting

Open a private security advisory or contact the maintainer privately before disclosing vulnerabilities.

## Deployment Warnings

- Never expose `/api/*` or `/data/*` without backend authentication.
- Never publish `.env`, `data/`, logs, generated sites, PM2 dumps, or Caddy basic-auth hashes.
- Rotate any token that has appeared in logs, process environments, shell history, screenshots, or chat transcripts.
- Generated sites must use a separate origin from the admin UI. Arbitrary site JavaScript can read dashboard credentials when both share an origin. Use the split-origin [Caddy example](examples/Caddyfile.example) and set the OAuth callback/base to the dashboard origin.
- Autofix executes generated regression tests only in a local Docker image (`MAXWELL_AUTOFIX_TEST_IMAGE`, default `maxwell:local`) with no network, no host credentials/socket, an unprivileged user, and resource limits. A passing regression is required before it pushes a topic branch. Keep this image trusted and rebuild it after dependency changes; unavailable isolation fails closed.
- The Maxwell service can control the host Docker daemon through `docker.sock`. A `:ro` socket mount, dropped capabilities, and `no-new-privileges` do not restrict Docker API calls or prevent host-root access through that daemon. Treat the bot service as host-root trusted; the separate shell/site containers have narrower mounts.
