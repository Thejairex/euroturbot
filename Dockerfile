FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Playwright browsers quedan en /root/.cache/ms-playwright
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# ── dependencias del sistema necesarias para Playwright/Chromium ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ──
WORKDIR /app
COPY automatizacion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ── Chromium + todas sus deps de sistema ──
RUN playwright install --with-deps chromium

# ── Código de la aplicación ──
# Se copia a /app/automatizacion/ para que los imports absolutos
# (from core..., from modules..., from config...) funcionen con
# WORKDIR=/app/automatizacion
COPY automatizacion/ /app/automatizacion/

# Crear directorios de datos para los volúmenes
RUN mkdir -p /app/automatizacion/input \
             /app/automatizacion/processed \
             /app/automatizacion/outputs/logs \
             /app/automatizacion/outputs/screenshots \
             /app/automatizacion/outputs/reports \
             /app/automatizacion/outputs/cache

WORKDIR /app/automatizacion

EXPOSE 8000

# Un solo worker (el pipeline usa threads internos; múltiples workers romperían el RunManager)
CMD ["uvicorn", "monitor.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
