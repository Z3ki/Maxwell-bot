# Maxwell itself. The shell sandbox image is docker/Dockerfile (maxwell-shell);
# site backends use docker/site-runtime/Dockerfile. This image is the bot + API.
FROM python:3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ffmpeg \
        libopus0 \
        libsodium23 \
        libsodium-dev \
        espeak-ng \
        nodejs \
        docker.io \
        chromium \
        fonts-liberation \
        fonts-dejavu-core \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-optional.txt ./
# discord-ext-voice-recv depends on official discord.py, which overwrites
# the discord.py-self fork. Reinstall the self-bot library last so `import
# discord` is the user-API wrapper Maxwell actually needs.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt -r requirements-optional.txt \
    && pip uninstall -y discord.py \
    && pip install --no-cache-dir --force-reinstall --no-deps "discord.py-self>=2.0.0"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Live checkout is bind-mounted over /app at runtime (see docker-compose.yml).
COPY . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAXWELL_IN_DOCKER=1

ENTRYPOINT ["/entrypoint.sh"]
