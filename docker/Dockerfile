FROM ubuntu:26.04

# Legacy shell sandbox Dockerfile (used by ShellTool with --network none etc).
# Legacy/general Dockerfile. Pi brain is integrated directly (Python bot + restricted `pi --mode rpc` subprocess).
# Pi-specific docker (pi-debian, entrypoint) removed.

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ca-certificates \
    ffmpeg \
    espeak-ng \
    jq \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash maxwell

USER maxwell
WORKDIR /home/maxwell

CMD ["sleep", "infinity"]
