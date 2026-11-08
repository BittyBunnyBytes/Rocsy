FROM python:3.12-slim
# apt over plain HTTP: deb.debian.org sometimes drops connections, which apt never retries (same shim as Video's).
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUS_HOST=0.0.0.0 \
    MUS_PORT=8200

WORKDIR /app
# ffmpeg: yt-dlp needs it to pull the audio out of a YouTube video (custom voices from a link) and to cut a stretch.
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/output /data/app /data/logs \
    && groupadd --system music && useradd --system --gid music --home /app music \
    && chown -R music:music /app /data
USER music

EXPOSE 8200
CMD ["sh", "-c", "uvicorn app:app --host ${MUS_HOST:-0.0.0.0} --port ${MUS_PORT:-8200}"]
