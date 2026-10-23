FROM cloudflare/cloudflared:latest AS cloudflared
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin connections
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY app.py cloudflare.py tunnels.py cloudflare_api.py telegram_comfyui_output.py ./
COPY static/ ./static/
USER connections
CMD ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8800"]
