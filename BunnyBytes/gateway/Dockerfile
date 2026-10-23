FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GATEWAY_HOST=127.0.0.1 \
    GATEWAY_PORT=8080

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bunnygate
COPY --chown=bunnygate:bunnygate . .
USER bunnygate

# MASTER_PASSWORD / COOKIE_SECRET are required and deliberately NOT baked in
# here -- config.py raises ConfigError at import time if either is unset, so
# the container fails to start rather than run insecurely. Pass them via the
# top-level compose file's environment: block, sourced from .env.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host ${GATEWAY_HOST:-127.0.0.1} --port ${GATEWAY_PORT:-8080}"]
