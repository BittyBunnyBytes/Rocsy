FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUS_HOST=0.0.0.0 \
    MUS_PORT=8200

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/output /data/app /data/logs \
    && groupadd --system music && useradd --system --gid music --home /app music \
    && chown -R music:music /app /data
USER music

EXPOSE 8200
CMD ["sh", "-c", "uvicorn app:app --host ${MUS_HOST:-0.0.0.0} --port ${MUS_PORT:-8200}"]
