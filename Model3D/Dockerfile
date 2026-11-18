FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py rig.py physics.py ./
COPY static ./static
CMD ["sh", "-c", "uvicorn app:app --host ${M3D_HOST:-127.0.0.1} --port ${M3D_PORT:-8320}"]
