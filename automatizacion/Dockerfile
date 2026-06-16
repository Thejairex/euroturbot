FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps chromium

# Copia el contenido del build context (./automatizacion) a /app/automatizacion/
# para que los imports absolutos (from core..., from config...) funcionen
COPY . /app/automatizacion/

RUN mkdir -p /app/automatizacion/input \
             /app/automatizacion/processed \
             /app/automatizacion/outputs/logs \
             /app/automatizacion/outputs/screenshots \
             /app/automatizacion/outputs/reports \
             /app/automatizacion/outputs/cache

WORKDIR /app/automatizacion

EXPOSE 8000

CMD ["uvicorn", "monitor.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
