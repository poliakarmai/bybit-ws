#!/usr/bin/env bash
# deploy.sh — АТОМАРНЫЙ деплой bybit-ws через symlink swap
# Использование: bash deploy.sh [--force]
set -euo pipefail

REPO=~/bybit-ws
LIVE_DIR=~/.local/lib/bybit_ws
STAGING_DIR="$LIVE_DIR.staging.$$"
VERSION=$(date +%Y%m%d_%H%M%S)

echo "🚀 Deploy bybit-ws @ $VERSION (atomic symlink swap)"

# ── 1. Проверить что репо чистый ──
cd "$REPO"
if ! git diff --quiet 2>/dev/null; then
    echo "⚠️  Незакоммиченные изменения. --force для принудительного деплоя."
    [ "${1:-}" = "--force" ] || exit 1
fi

# ── 2. Smoke-тесты ──
echo "🧪 Smoke-тесты..."
python3 test_smoke.py || {
    echo "❌ Smoke-тесты провалились — деплой отменён"
    exit 1
}

# ── 3. Копируем в staging-директорию ──
echo "📦 Копирование в staging..."
rm -rf "$STAGING_DIR"
cp -r "$REPO/bybit_ws" "$STAGING_DIR"
# Копируем корневые скрипты
for f in sl_reentry.py entry_judge.py; do
    [ -f "$REPO/$f" ] && cp "$REPO/$f" "$STAGING_DIR/"
done
echo "   Staging: $STAGING_DIR"

# ── 4. Атомарный swap (symlink) ──
echo "🔄 Атомарный swap..."
# Создаём новый symlink во временной директории
OLD_LINK=$(readlink -f "$LIVE_DIR" 2>/dev/null || echo "$LIVE_DIR")
ln -sfn "$STAGING_DIR" "$LIVE_DIR.new"
mv "$LIVE_DIR.new" "$LIVE_DIR" 2>/dev/null || {
    # Если mv не сработал (разные FS), делаем через rm + ln
    rm -f "$LIVE_DIR"
    ln -s "$STAGING_DIR" "$LIVE_DIR"
}
echo "   Symlink: $LIVE_DIR → $STAGING_DIR"

# ── 5. Рестарт ──
echo "🔄 Рестарт сервиса..."
systemctl --user kill -s SIGKILL bybit-ws-async 2>/dev/null || true
sleep 2
systemctl --user start bybit-ws-async
sleep 5

# ── 6. Canary monitoring ──
HEALTH_FILE=~/.local/share/bybit-ws/health.txt
MAX_CHECKS=8
MAX_HEALTH_AGE=60

echo "🐤 Canary: $MAX_CHECKS checks..."
for i in $(seq 1 $MAX_CHECKS); do
    sleep 5
    if ! systemctl --user is-active bybit-ws-async > /dev/null 2>&1; then
        echo "❌ Check $i: service NOT active — ROLLBACK"
        # Rollback: symlink back
        [ -n "$OLD_LINK" ] && [ -d "$OLD_LINK" ] && {
            rm -f "$LIVE_DIR"
            ln -s "$OLD_LINK" "$LIVE_DIR"
            systemctl --user restart bybit-ws-async
            echo "↩️  Rollback to $OLD_LINK"
        }
        rm -rf "$STAGING_DIR"
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

# ── 7. Очистка старых staging-директорий ──
find "$(dirname "$LIVE_DIR")" -maxdepth 1 -name 'bybit_ws.staging.*' -mtime +1 -exec rm -rf {} \; 2>/dev/null || true

echo "✅ Деплой успешен"
echo "   Версия: $(git -C "$REPO" rev-parse --short HEAD)"
echo "   Live: $LIVE_DIR → $(readlink -f "$LIVE_DIR")"
exit 0
