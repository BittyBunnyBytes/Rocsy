# Rocsy Motion: code-based video (Remotion 4, React). The Master Agent writes a project in its
# workspace; this renders it: frames in Chrome for Testing with GPU GL (ANGLE/Vulkan on the
# RX 7900 XTX through Mesa), audio as WAV, then ffmpeg encodes on the GPU (h264_vaapi).
FROM node:22-trixie-slim
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get
ENV NODE_ENV=production
RUN apt-get update && apt-get install --no-install-recommends -y \
      ffmpeg mesa-va-drivers mesa-vulkan-drivers libvulkan1 libegl1 libgl1 libgbm1 libdrm2 \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
      libxfixes3 libasound2 libpango-1.0-0 libcairo2 libx11-xcb1 libxshmfence1 \
      fonts-dejavu fonts-liberation fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund --omit=dev && npm cache clean --force
# The GPU-capable browser (headless-shell can't use the GPU drivers on Linux).
RUN node -e "import('@remotion/renderer').then((r) => r.ensureBrowser({ chromeMode: 'chrome-for-testing' }))" \
    && test -n "$(find /app/node_modules/.remotion -name chrome -type f)"
COPY server.mjs ./
COPY template ./template
RUN mkdir -p /data && chown -R node:node /data /app
USER node
EXPOSE 8970
CMD ["node", "server.mjs"]
