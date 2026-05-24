FROM ubuntu:26.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    dnsutils \
    nmap \
    whois \
    netcat-openbsd \
    traceroute \
    iputils-ping \
    python3 \
    python3-pip \
    ca-certificates \
    git \
    jq \
    bsdmainutils \
    file \
    sudo \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash maxwell \
    && echo "maxwell ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/maxwell \
    && chmod 0440 /etc/sudoers.d/maxwell

USER maxwell
WORKDIR /home/maxwell

CMD ["sleep", "infinity"]
