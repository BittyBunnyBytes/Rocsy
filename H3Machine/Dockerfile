FROM python:3.12-slim
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    H3_HOST=0.0.0.0 \
    H3_PORT=8000

WORKDIR /app
# ffprobe (from ffmpeg) reads reference video/audio upload durations for the
# MiniMax H3 reference-limit checks (see app.py's _probe_duration_seconds).
# mesa-va-drivers: VA-API for the host's AMD GPU (radeonsi), so ffmpeg encodes on
# the GPU (h264_vaapi) instead of the CPU - needs /dev/dri/renderD128 mounted.
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg mesa-va-drivers \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/uploads /data/input /data/output /data/app /data/logs \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin h3web \
    && chown -R h3web:h3web /app /data

USER h3web
EXPOSE 8000
# exec: uvicorn must be PID 1 to get SIGTERM - under a plain `sh -c` it never
# did, so every stop/deploy waited Docker's 10 s and SIGKILLed it, skipping the
# shutdown cleanup (GPU-lock token release). Open event streams (/api/events)
# never finish on their own, so give queued enhancement cleanup 30 s rather than waiting forever.
CMD ["sh", "-c", "exec uvicorn app:app --host ${H3_HOST:-0.0.0.0} --port ${H3_PORT:-8000} --timeout-graceful-shutdown 30"]
