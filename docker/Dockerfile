FROM ubuntu:26.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ca-certificates \
    curl \
    wget \
    git \
    vim \
    nano \
    htop \
    iputils-ping \
    net-tools \
    dnsutils \
    build-essential \
    sudo \
    ffmpeg \
    espeak-ng \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Allow root for full filesystem access when requested. Home dir kept for compatibility.
RUN useradd -m -s /bin/bash maxwell || true
RUN echo "maxwell ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers || true

USER root
WORKDIR /home/maxwell

CMD ["sleep", "infinity"]
