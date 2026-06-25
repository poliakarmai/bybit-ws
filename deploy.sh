#!/usr/bin/env bash
# deploy.sh — деплой bybit-ws из git-репо (сервис читает напрямую из ~/bybit-ws/bybit_ws/)
# Использование: bash deploy.sh [--force]
set -euo pipefail

REPO=~/bybit-ws
VERSION=$(date +%Y%m%d_%H%M%S)

echo "🚀 Deploy bybit-ws @ $VERSION"

# ── 1. Проверить что репо чистый и актуальный ──
cd "$REPO"
if ! git diff --quiet 2>/dev/null; then
    echo "⚠️  Есть незакоммиченные изменения. Используйте --force для принудительного деплоя."
    [ "${1:-}" = "--force" ] || exit 1
fi

# ── 2. VirusTotal scan ──
VT_SCANNER=~/.hermes/scripts/vt-scan.py
if [ -f "$VT_SCANNER" ] && [ -n "${VT_API_KEY:-}" ]; then
    echo "🛡️ VirusTotal scan..."
    python3 "$VT_SCANNER" "$REPO" || {
        echo "⚠️  VirusTotal found suspicious files — review before deploying"
        [ "${SKIP_VT_SCAN:-0}" = "1" ] || exit 1
    }
fi

# ── 3. Smoke-тесты ──
echo "🧪 Smoke-тесты..."
source .venv/bin/activate
python3 test_smoke.py || {
    echo "❌ Smoke-тесты провалились — деплой отменён"
    exit 1
}

# ── 4. Рестарт (SIGKILL — asyncio не отпускает по SIGTERM) ──
echo "🔄 Рестарт сервиса..."
systemctl --user kill -s SIGKILL bybit-ws-async 2>/dev/null || true
sleep 2
systemctl --user start bybit-ws-async
sleep 5

# ── 5. Canary monitoring (10 mins, 20 checks every 30s) ──
HEALTH_FILE=~/.local/share/bybit-ws/health.txt
MAX_CHECKS=20
CHECK_INTERVAL=30
MAX_HEALTH_AGE=300

echo "🐤 Canary monitoring: $MAX_CHECKS checks every ${CHECK_INTERVAL}s..."

for i in $(seq 1 $MAX_CHECKS); do
    sleep $CHECK_INTERVAL

    if ! systemctl --user is-active bybit-ws-async > /dev/null 2>&1; then
        echo "❌ Check $i/$MAX_CHECKS: service NOT active — MANUAL ROLLBACK REQUIRED"
        echo "   git checkout <previous-commit> && bash deploy.sh"
        exit 1
    fi

    if [ ! -f "$HEALTH_FILE" ]; then
        echo "❌ Check $i/$MAX_CHECKS: health.txt NOT FOUND"
        exit 1
    fi

    health_age=$(($(date +%s) - $(stat -c %Y "$HEALTH_FILE")))
    if [ "$health_age" -gt "$MAX_HEALTH_AGE" ]; then
        echo "❌ Check $i/$MAX_CHECKS: health.txt age ${health_age}s > ${MAX_HEALTH_AGE}s"
        exit 1
    fi

    echo "  ✅ Check $i/$MAX_CHECKS: active, health_age=${health_age}s"
done

echo "✅ Canary passed — деплой успешен"
echo "   Версия: $(git rev-parse --short HEAD)"
echo "   PID: $(systemctl --user show bybit-ws-async -p MainPID | cut -d= -f2)"
exit 0
