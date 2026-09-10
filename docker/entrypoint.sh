#!/usr/bin/env bash
set -euo pipefail
cd /app

mkdir -p data public/bot shelldocker logs data/exports data/site_servers

exec python3 docker/supervisor.py "$@"
