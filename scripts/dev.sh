#!/usr/bin/env bash
# All Docker operations are scoped to the isolated dev project.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
fail() { printf 'dev: %s\n' "$*" >&2; exit 1; }
GIT_DIR=$(git rev-parse --absolute-git-dir) || fail 'Not a Git worktree.'
COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir)
[[ "$GIT_DIR" != "$COMMON_DIR" ]] || fail 'Use a linked dev worktree, not the primary checkout.'
[[ "$ROOT" != /root/maxwell ]] || fail 'Refusing the production checkout.'
BRANCH=$(git symbolic-ref --quiet --short HEAD) || fail 'Use dev or a feature branch, not a detached checkout.'
[[ "$BRANCH" == dev || "$BRANCH" == feature/* ]] || fail 'Use dev or feature/*, never main or another production branch.'
COMMAND=${1:-status}
if (($#)); then shift; fi
case "$COMMAND" in up|restart|test|status|logs|stop|youtube-auth) ;; *) fail 'Usage: bash scripts/dev.sh {up|restart|test|status|logs|stop|youtube-auth} [pytest arguments]' ;; esac
[[ "$COMMAND" == test || $# == 0 ]] || fail 'Only test accepts extra arguments.'

if [[ "$COMMAND" == test ]]; then
    # No Compose envfile, network, persistent volumes or inherited provider keys.
    docker build -f docker/maxwell.Dockerfile -t maxwell:dev-test-runtime .
    # Repository inspection tests need the same real rg binary as CI.
    docker build -t maxwell:dev-test-local - <<'DOCKERFILE'
FROM maxwell:dev-test-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*
DOCKERFILE
    masks=()
    for secret in .env .env.dev; do
        if [[ -f "$ROOT/$secret" ]]; then
            masks+=(--mount "type=bind,src=/dev/null,dst=/app/$secret,readonly")
        fi
    done
    # A linked worktree's real .git points outside this mount. Supply empty,
    # disposable metadata instead of exposing production Git history/config.
    git_pointer=$(mktemp)
    trap 'rm -f -- "$git_pointer"' EXIT
    printf 'gitdir: /tmp/maxwell-test-git\n' > "$git_pointer"
    docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --no-healthcheck \
        --mount "type=bind,src=$ROOT,dst=/app,readonly" "${masks[@]}" \
        --mount "type=bind,src=$git_pointer,dst=/app/.git,readonly" \
        --tmpfs /tmp:rw,nosuid,nodev,exec,size=1g \
        -e MAXWELL_ENV_FILE=/tmp/maxwell-test-env-not-created \
        -e DATA_DIR=/tmp/maxwell-test-data -e LOGS_DIR=/tmp/maxwell-test-logs \
        -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
        -w /app --entrypoint bash maxwell:dev-test-local -c '
            git init -q --separate-git-dir=/tmp/maxwell-test-git /tmp/maxwell-test-worktree
            git --git-dir=/tmp/maxwell-test-git config core.worktree /app
            exec python3 -m pytest -p no:cacheprovider -W error::RuntimeWarning "$@"
        ' -- "$@"
    exit
fi

[[ -f .env.dev ]] || fail 'Create the isolated .env.dev first (see docs/DEV_DOCKER.md).'
[[ ${WITH_MUSIC:-0} == 0 || ${WITH_MUSIC:-0} == 1 ]] || fail 'WITH_MUSIC must be 0 or 1.'
[[ ${LOCAL_AI:-0} == 0 || ${LOCAL_AI:-0} == 1 ]] || fail 'LOCAL_AI must be 0 or 1.'
# Ambient shell variables override --env-file during Compose interpolation.
# Scrub every name .env.dev defines so the file's own values always win —
# only variable NAMES are read here, never values.
while IFS= read -r key; do
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    unset "$key"
done < <(sed -n 's/^[[:space:]]*\(export[[:space:]]*\)\?\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' .env.dev)
COMPOSE=(docker compose --project-name maxwell-dev --env-file .env.dev -f docker-compose.dev.yml)
if [[ ${WITH_MUSIC:-0} == 1 ]]; then COMPOSE+=(-f docker-compose.dev.music.yml); fi
if [[ ${LOCAL_AI:-0} == 1 ]]; then COMPOSE+=(--profile local-ai); fi
# Resolve with Compose's dotenv semantics; never source an operator env file as code.
# Capture configuration in memory, and print only validation errors, never values.
"${COMPOSE[@]}" config --format json | python3 -c '
import json, sys
from pathlib import Path
settings = {}
for line in Path(".env.dev").read_text().splitlines():
    key, separator, value = line.strip().removeprefix("export ").partition("=")
    if separator:
        settings[key.strip()] = value.strip().strip("\"").strip(chr(39))
if settings.get("MAXWELL_DEV_MODE", "").lower() not in ("true", "1", "yes", "on"):
    print("dev: Enable MAXWELL_DEV_MODE explicitly in .env.dev.", file=sys.stderr)
    sys.exit(1)
config = json.load(sys.stdin)
env = config["services"]["maxwell-dev"]["environment"]
def reject(message):
    print("dev: " + message, file=sys.stderr)
    sys.exit(1)
if str(env.get("MAXWELL_DEV_MODE", "")).lower() not in ("true", "1", "yes", "on"):
    reject("MAXWELL_DEV_MODE must be enabled.")
if not str(env.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_TOKEN") or "").strip():
    reject("Configure a separate dev DISCORD_BOT_TOKEN.")
ids = str(env.get("MAXWELL_DEV_GUILD_IDS", "")).split(",")
if not ids or any(not value.strip().isdecimal() or int(value.strip()) <= 0 for value in ids):
    reject("Configure numeric allowlisted MAXWELL_DEV_GUILD_IDS.")
'
case "$COMMAND" in
    up|restart)
        mkdir -p data logs public/bot shelldocker
        # Build only the bot and selected dev audio node. Dependency services
        # start through Compose without recreating healthy model/audio containers.
        services=(maxwell-dev)
        if [[ ${WITH_MUSIC:-0} == 1 ]]; then services+=(lavalink-dev); fi
        "${COMPOSE[@]}" build --build-arg "MAXWELL_BUILD_COMMIT=$(git rev-parse HEAD)" "${services[@]}"
        if [[ ${LOCAL_AI:-0} == 1 ]]; then "${COMPOSE[@]}" up -d ollama-dev; fi
        if [[ ${WITH_MUSIC:-0} == 1 ]]; then
            "${COMPOSE[@]}" up -d --wait lavalink-dev
            # A replacement node has a new namespace even when the cipher's
            # own settings are unchanged. Never retain its old namespace.
            "${COMPOSE[@]}" up -d --force-recreate --no-deps --wait yt-cipher-dev
        fi
        "${COMPOSE[@]}" up -d --force-recreate --no-deps maxwell-dev
        ;;
    status) "${COMPOSE[@]}" ps --all ;;
    youtube-auth) python3 scripts/youtube_auth.py --env-file .env.dev ;;
    logs) "${COMPOSE[@]}" logs --follow --tail 100 maxwell-dev ;;
    stop) "${COMPOSE[@]}" --profile local-ai stop ;;
esac
