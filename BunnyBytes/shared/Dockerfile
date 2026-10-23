FROM python:3.12-slim
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py operations.py agentic.py ./
COPY guides ./guides
RUN useradd --system --create-home --uid 10001 framework
RUN mkdir -p /data && chown framework:framework /data
USER framework
CMD ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8700"]
