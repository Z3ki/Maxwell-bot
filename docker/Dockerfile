# syntax=docker/dockerfile:1
# Shell-tool sandbox (image name: maxwell-shell).
# The bot itself is docker/maxwell.Dockerfile, started via docker-compose.yml.
#
# Isolated runs (the default) cap memory/CPU/pids and bind-mount only
# shelldocker/. The process is root with full capabilities — isolation is
# the mount and the network, not cap-drop. This image stays small and has
# no sudo binary.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
        curl \
        wget \
        git \
        jq \
        ffmpeg \
        espeak-ng \
        iputils-ping \
        dnsutils \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash -u 1000 maxwell \
    && mkdir -p /home/maxwell \
    && chown maxwell:maxwell /home/maxwell

WORKDIR /home/maxwell
# Root with full capabilities inside the sandbox. Isolation is the bind
# mount (shelldocker/ only unless MAXWELL_SHELL_FULL_HOST) and bridge net.
USER root

CMD ["sleep", "infinity"]
