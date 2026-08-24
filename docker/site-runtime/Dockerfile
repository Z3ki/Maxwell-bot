# Runtime for site backends written by Maxwell (see site_server.py).
#
# Deliberately small and boring: one Python with the three libraries a small
# web service actually needs, and nothing else. Each site gets its own
# container from this image — read-only code at /app, its own writable /data,
# no capabilities, half a core, and a port published only on loopback.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    flask==3.1.0 \
    waitress==3.0.2 \
    requests==2.32.3

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
