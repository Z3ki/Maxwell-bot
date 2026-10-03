# syntax=docker/dockerfile:1
# Runtime for site backends written by Maxwell (see site_server.py).
#
# New apps use one standard stack: FastAPI/Uvicorn, one worker without reload,
# and SQLite in /data. Flask/waitress remain installed to preserve existing
# apps. This shared image is built once by the operator, never by generated
# code; existing per-site dependency images may be reused but not rebuilt.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    HOME=/tmp

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    # request/response
    flask==3.1.0 \
    waitress==3.0.2 \
    # websockets / async — this is what multiplayer runs on
    fastapi==0.115.6 \
    "uvicorn[standard]==0.34.0" \
    websockets==14.1 \
    python-multipart==0.0.20 \
    # accounts: password hashing and signed tokens
    bcrypt==4.2.1 \
    pyjwt==2.10.1 \
    itsdangerous==2.2.0 \
    # data
    sqlalchemy==2.0.36 \
    # outbound + rendering + media
    requests==2.32.3 \
    httpx==0.28.1 \
    jinja2==3.1.5 \
    pillow==11.1.0

# Unprivileged. The code mount is read-only; /data is the only writable path
# that survives a restart.
RUN useradd -m -u 10001 site \
    && mkdir -p /data \
    && chown site:site /data

USER site
WORKDIR /app
EXPOSE 8000
CMD ["python", "app.py"]
