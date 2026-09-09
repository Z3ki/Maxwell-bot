# syntax=docker/dockerfile:1
# Shell-tool sandbox (image name: maxwell-shell).
# The bot itself is docker/maxwell.Dockerfile, started via docker-compose.yml.
#
# Isolated runs (the default) drop all caps, set no-new-privileges, cap
# memory/CPU/pids, and bind-mount only shelldocker/. This image stays small
# and has no sudo binary. Root is the default user so `apt-get` inside the
# sandbox still works under the remaining capabilities.

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
# Root so package installs in the sandbox still work; Compose/run flags
# (cap-drop ALL, no-new-privileges) are the isolation wall.
USER root

CMD ["sleep", "infinity"]
