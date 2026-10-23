FROM python:3.11-slim
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# The host has ROCm 7.2. Install the matching PyTorch wheel before qwen-tts so
# pip does not substitute a CUDA wheel. This installs code, never model weights.
RUN pip install --no-cache-dir torch==2.11.0 torchaudio==2.11.0 \
    --index-url https://download.pytorch.org/whl/rocm7.2
RUN pip install --no-cache-dir fastapi==0.143.5 starlette==1.7.0 uvicorn==0.34.2 httpx==0.28.1 \
    qwen-tts==0.1.1 soundfile yt-dlp
# Gradio currently caps Pillow below the release with the image-decoder fixes.
# Rocsy calls qwen-tts directly and does not use the Gradio UI.
RUN pip install --no-cache-dir --no-deps Pillow==13.0.0
RUN apt-get update && apt-get install -y --no-install-recommends sox ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py reference.py settings.py index.html ./
COPY static ./static
EXPOSE 8950
CMD ["sh", "-c", "uvicorn app:app --host 127.0.0.1 --port ${VOICE_PORT:-8950}"]
