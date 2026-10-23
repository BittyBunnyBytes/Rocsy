FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi==0.143.5 starlette==1.7.0 uvicorn==0.34.2 httpx==0.28.1
WORKDIR /app
COPY *.py ./
COPY static ./static
EXPOSE 8960
CMD ["sh", "-c", "uvicorn app:app --host 127.0.0.1 --port ${MODELHUB_PORT:-8960}"]
