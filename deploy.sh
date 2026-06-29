#!/usr/bin/env bash
# deploy.sh — деплой bybit-ws: тесты → рестарт → canary
# Сервис работает из WorkingDirectory (~/bybit-ws), без staging-копий.
# Использование: bash deploy.sh [--force]
set -euo pipefail

REPO=~/bybit-ws
VERSION=$(date +%Y%m%d_%H%M%S)

echo "🚀 Deploy bybit-ws @ $VERSION"

# ── 1. Проверить что репо чистый ──
cd "$REPO"
if ! git diff --quiet 2>/dev/null; then
    echo "⚠️  Незакоммиченные изменения. --force для принудительного деплоя."
    [ "${1:-}" = "--force" ] || exit 1
fi

# ── 2. Логическая целостность ──
echo "🧠 Logic integrity tests..."
python3 test_logic_integrity.py || {
    echo "❌ Logic integrity failed — деплой отменён"
    exit 1
}

# ── 3. Smoke-тесты ──
echo "🧪 Smoke-тесты..."
python3 test_smoke.py || {
    echo "❌ Smoke-тесты провалились — деплой отменён"
    exit 1
}

# ── 4. Graceful рестарт ──
echo "🔄 Рестарт сервиса..."
systemctl --user stop bybit-ws-async
for i in $(seq 1 10); do
    systemctl --user is-active bybit-ws-async > /dev/null 2>&1 || break
    sleep 1
done
systemctl --user is-active bybit-ws-async > /dev/null 2>&1 && {
    echo "⚠️ SIGTERM не сработал — SIGKILL"
    systemctl --user kill -s SIGKILL bybit-ws-async 2>/dev/null || true
    sleep 2
}
systemctl --user start bybit-ws-async
sleep 5

# ── 5. Canary monitoring ──
HEALTH_FILE=~/.local/share/bybit-ws/health.txt
MAX_CHECKS=8
MAX_HEALTH_AGE=60

echo "🐤 Canary: $MAX_CHECKS checks..."
for i in $(seq 1 $MAX_CHECKS); do
    sleep 5
    if ! systemctl --user is-active bybit-ws-async > /dev/null 2>&1; then
        echo "❌ Check $i: service NOT active"
        exit 1
    fi

    if [ ! -f "$HEALTH_FILE" ]; then
        echo "❌ Check $i: health.txt NOT FOUND"
        exit 1
    fi

    health_age=$(($(date +%s) - $(stat -c %Y "$HEALTH_FILE")))
    if [ "$health_age" -gt "$MAX_HEALTH_AGE" ]; then
        echo "❌ Check $i: health age ${health_age}s > ${MAX_HEALTH_AGE}s"
        exit 1
    fi

    echo "  ✅ Check $i: OK (health_age=${health_age}s)"
done

echo "✅ Деплой успешен"
echo "   Версия: $(git -C "$REPO" rev-parse --short HEAD)"
exit 0
