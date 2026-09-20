FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl wget git jq unzip zip xz-utils file ripgrep \
    python3 python3-pip python3-venv python3-dev \
    build-essential cmake ninja-build pkg-config \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 maxwell || true
WORKDIR /workspace
CMD ["bash", "-lc", "trap : TERM INT; sleep infinity & wait"]
