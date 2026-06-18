#!/usr/bin/env bash
# deploy.sh — атомарный деплой bybit-ws с rollback
# Использование: bash deploy.sh [--force]
set -euo pipefail

REPO=~/bybit-ws
TARGET=~/.local/lib/bybit_ws
VERSION=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=$TARGET.bak_$VERSION
FILES=(auto_entry.py api.py rl_agent.py ensemble.py rpc.py lstm_regime.py ml_scorer.py auto_sl.py auto_short.py)

# ── 1. Бэкап ──
echo "📦 Бэкап в $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
for f in "${FILES[@]}"; do
    [ -f "$TARGET/$f" ] && cp "$TARGET/$f" "$BACKUP_DIR/" || true
done

# ── 2. Атомарный swap ──
echo "🔄 Атомарный деплой..."
FAILED=()
for f in "${FILES[@]}"; do
    if [ -f "$REPO/$f" ]; then
        cp "$REPO/$f" "$TARGET/$f.new"
        mv "$TARGET/$f.new" "$TARGET/$f" || FAILED+=("$f")
    fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "❌ Atomic swap failed for: ${FAILED[*]}"
fi

# ── 3. Рестарт ──
echo "🔄 Рестарт сервиса..."
systemctl --user restart bybit-ws-async
sleep 5

# ── 4. Smoke test ──
echo "🩺 Smoke test..."
if curl -sf http://127.0.0.1:8766/health > /dev/null 2>&1; then
    echo "✅ Деплой успешен (PID $(systemctl --user show bybit-ws-async -p MainPID | cut -d= -f2))"
    echo "   Бэкап: $BACKUP_DIR"
    exit 0
fi

# ── 5. Rollback ──
echo "❌ Smoke test failed — ROLLBACK"
for f in "${FILES[@]}"; do
    [ -f "$BACKUP_DIR/$f" ] && cp "$BACKUP_DIR/$f" "$TARGET/" || true
done
systemctl --user restart bybit-ws-async
sleep 3
echo "🔄 Откат завершён. Статус:"
systemctl --user status bybit-ws-async --no-pager -l | head -5
exit 1
