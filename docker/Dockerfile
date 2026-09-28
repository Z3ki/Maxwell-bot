# syntax=docker/dockerfile:1
# Shell-tool sandbox (image name: maxwell-shell).
# The bot itself is docker/maxwell.Dockerfile, started via docker-compose.yml.
#
# Public shell runs only under the configured gVisor (runsc) Docker runtime.
# Root is available inside the guest with a small allow-list of guest
# capabilities. The host applies no-new-privileges, resource limits, and
# egress filtering. This image stays small and has no sudo binary.

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

RUN mkdir -p /workspace

WORKDIR /workspace
# The uid is root in the isolated guest, without host capabilities.
USER root

CMD ["sleep", "infinity"]
