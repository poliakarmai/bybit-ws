#!/bin/bash
set -e

CONFIG_DIR="/root/.config/bybit-ws"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
EXAMPLE_CONFIG="$CONFIG_DIR/config.example.yaml"

# Если конфига нет — копируем example
if [ ! -f "$CONFIG_FILE" ]; then
    echo "⚠️  config.yaml не найден. Копирую config.example.yaml."
    echo "   Настройте $CONFIG_FILE и перезапустите."
    cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
fi

echo "🚀 Bybit Bollinger Grid Monitor v3.3"
echo "   RPC: http://0.0.0.0:8766"
echo "   Health: http://0.0.0.0:8766/health"

exec python3 -m bybit_ws.main
