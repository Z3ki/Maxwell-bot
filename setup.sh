#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--no-input" ]; then
  exec bash ./install.sh --local --non-interactive
fi

exec bash ./install.sh --local "$@"
