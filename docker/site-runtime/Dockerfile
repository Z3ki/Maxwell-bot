# Runtime for site backends written by Maxwell (see site_server.py).
#
# Sized for what people actually build: a site with accounts, a database, and
# other people connected to it at the same time. Both server styles are here —
# Flask/waitress for plain request/response, FastAPI/uvicorn when the app needs
# WebSockets — and the app picks by writing whichever one it wants in app.py.
#
# A site that needs something not in this list passes `packages` to
# site_server, which builds a per-site image FROM this one.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
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
RUN useradd -m -u 10001 site
RUN mkdir -p /data && chown site:site /data

USER site
WORKDIR /app
ENV PORT=8000 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

EXPOSE 8000
CMD ["python", "app.py"]
