FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KREA_HOST=0.0.0.0 \
    KREA_PORT=8300

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/output /data/app /data/logs \
    && groupadd --system krea && useradd --system --gid krea --home /app krea \
    && chown -R krea:krea /app /data
USER krea

EXPOSE 8300
CMD ["sh", "-c", "uvicorn app:app --host ${KREA_HOST:-0.0.0.0} --port ${KREA_PORT:-8300}"]
