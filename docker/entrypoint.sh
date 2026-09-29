#!/usr/bin/env bash
set -euo pipefail
cd /app

# Production accepts main, a pinned detached checkout, or the source bundled
# into a release image (which has no .git). Named development branches remain
# opt-in so a bind-mounted dev checkout cannot silently become production.
if [ -e .git ] && [ "${MAXWELL_DEV_MODE:-}" != "true" ] && [ "${MAXWELL_DEV_MODE:-}" != "1" ]; then
  branch="$(git rev-parse --abbrev-ref HEAD)"
  case "$branch" in
    main|HEAD) ;;
    *) echo "Refusing production startup from development branch: $branch" >&2; exit 1 ;;
  esac
fi

# Explicit commands support diagnostics and release-image smoke checks without
# accidentally starting a second bot or forwarding commands as bot arguments.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

mkdir -p data public/bot shelldocker logs data/exports data/site_servers

exec python3 docker/supervisor.py "$@"
