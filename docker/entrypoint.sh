#!/usr/bin/env bash
set -euo pipefail
cd /app

mkdir -p data public/bot shelldocker logs data/exports data/site_servers

if [ "${MAXWELL_START_API:-1}" != "0" ]; then
  python3 api/api_server.py &
fi

exec python3 bot.py "$@"
