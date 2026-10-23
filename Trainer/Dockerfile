FROM rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.9.1
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRAINER_HOST=127.0.0.1 \
    TRAINER_PORT=8500 \
    HF_HOME=/data/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
      git git-lfs curl ca-certificates build-essential python3.12-dev python3.12-venv \
      libnuma-dev libgl1 libglib2.0-0 ffmpeg mesa-va-drivers vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt
# Headless Chromium for the clip finder (JavaScript-only sites); browsers live outside /app.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python3 -m playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY . .

RUN mkdir -p /data /datasets /models /opt/trainers
EXPOSE 8500
CMD ["sh", "-c", "exec python3 -m uvicorn app:app --host \"$TRAINER_HOST\" --port \"$TRAINER_PORT\""]
