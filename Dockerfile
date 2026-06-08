FROM python:3.11-slim

LABEL org.opencontainers.image.title="Bybit Bollinger Grid Monitor"
LABEL org.opencontainers.image.description="Автономный трейдинг-монитор с REST API для AI-агентов. LONG + SHORT стратегии, 24/7."
LABEL org.opencontainers.image.version="3.3"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код
COPY bybit_ws/ bybit_ws/
COPY bollinger_scanner.py .

# Конфиг (монтируется снаружи)
RUN mkdir -p /root/.config/bybit-ws
COPY config.example.yaml /root/.config/bybit-ws/config.example.yaml

# Точка входа
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8766/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
