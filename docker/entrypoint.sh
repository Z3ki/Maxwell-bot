#!/usr/bin/env bash
set -euo pipefail
cd /app

# Production always runs main. The Dev container sets MAXWELL_DEV_MODE and
# is allowed to run the dev checkout.
if [ "${MAXWELL_DEV_MODE:-}" != "true" ] && [ "${MAXWELL_DEV_MODE:-}" != "1" ]; then
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ "$branch" != "main" ]; then
    echo "Refusing to start: production Maxwell must run branch main (checked out: ${branch:-unknown})" >&2
    exit 1
  fi
fi

mkdir -p data public/bot shelldocker logs data/exports data/site_servers

exec python3 docker/supervisor.py "$@"
