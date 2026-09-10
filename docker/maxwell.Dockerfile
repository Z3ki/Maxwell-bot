# syntax=docker/dockerfile:1
# Maxwell itself. The shell sandbox image is docker/Dockerfile (maxwell-shell);
# site backends use docker/site-runtime/Dockerfile. This image is the bot + API.
#
# Multi-stage: compile wheels in `deps`, keep gcc/headers out of the runtime.
# slim-bookworm + BuildKit caches keep rebuilds fast; the live checkout is
# still bind-mounted over /app at runtime (see docker-compose.yml).
# The process runs as root because Compose bind-mounts docker.sock so sibling
# containers (shell, site backends) can be spawned. Strength comes from
# cap_drop / no-new-privileges / resource limits in Compose, not a fake USER.

FROM docker:28-cli AS docker-cli

FROM python:3.12-slim-bookworm AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libsodium-dev

WORKDIR /app
COPY requirements.txt requirements-optional.txt requirements-dev.txt ./

# discord-ext-voice-recv depends on official discord.py, which overwrites
# the discord.py-self fork. Reinstall the self-bot library last so `import
# discord` is the user-API wrapper Maxwell actually needs.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install -r requirements.txt -r requirements-optional.txt -r requirements-dev.txt \
    && pip uninstall -y discord.py \
    && pip install --force-reinstall --no-deps "discord.py-self>=2.0.0"


FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAXWELL_IN_DOCKER=1 \
    CHROME_PATH=/usr/bin/chromium

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ffmpeg \
        libopus0 \
        libsodium23 \
        espeak-ng \
        nodejs \
        chromium \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /usr/local /usr/local
COPY --from=docker-cli /usr/local/bin/docker /usr/bin/docker

WORKDIR /app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh

# Live checkout is bind-mounted over /app at runtime (see docker-compose.yml).
# Use the bundled Linux CLI on every host; host binaries may be absent at
# /usr/bin/docker or depend on libraries unavailable in this image.
COPY . /app

ENTRYPOINT ["/entrypoint.sh"]

# API is started by the entrypoint unless MAXWELL_START_API=0. Bot process
# presence is the real liveness signal (dashboard may be off).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "/app/docker/healthcheck.py"]
