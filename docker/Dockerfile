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
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash maxwell

USER maxwell
WORKDIR /home/maxwell

CMD ["sleep", "infinity"]
