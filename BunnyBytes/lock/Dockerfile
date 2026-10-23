FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOCK_PORT=8600

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bunnylock
COPY --chown=bunnylock:bunnylock . .
USER bunnylock

# bunny-lock binds 127.0.0.1 only, per network_mode: host. It now has two
# trust surfaces: an internal one (loopback + X-Queue-Internal-Secret --
# /enqueue, /heartbeat, /release, /dequeue, PATCH /queue/*), called only by
# the four apps' own backend code, and a browser-facing one (/Queue/... --
# the queue page, GET /api/queue, GET /api/queue/events, POST
# /api/queue/{id}/cancel) reached ONLY via bunny-gateway proxying over
# loopback, which gates it with the same session cookie as every other app.
EXPOSE 8600
CMD ["sh", "-c", "uvicorn app:app --host 127.0.0.1 --port ${LOCK_PORT:-8600}"]
