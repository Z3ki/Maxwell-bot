FROM ghcr.io/lavalink-devs/lavalink:4.2.2
USER root
# Authenticated health probe uses curl; application never exposes its port publicly.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
USER lavalink
