#!/usr/bin/env bash
# deploy.sh — атомарный деплой bybit-ws с rollback
# Использование: bash deploy.sh [--force]
set -euo pipefail

REPO=~/bybit-ws
TARGET=~/.local/lib/bybit_ws
VERSION=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=$TARGET.bak_$VERSION
FILES=(auto_entry.py api.py rl_agent.py ensemble.py rpc.py lstm_regime.py ml_scorer.py auto_sl.py auto_short.py ws_client.py optuna_tuner.py)

# ── 1. Бэкап ──
echo "📦 Бэкап в $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
for f in "${FILES[@]}"; do
    [ -f "$TARGET/$f" ] && cp "$TARGET/$f" "$BACKUP_DIR/" || true
done

# ── 2. VirusTotal scan ──
VT_SCANNER=~/.hermes/scripts/vt-scan.py
if [ -f "$VT_SCANNER" ] && [ -n "${VT_API_KEY:-}" ]; then
    echo "🛡️ VirusTotal scan..."
    python3 "$VT_SCANNER" "$REPO" || {
        echo "⚠️  VirusTotal found suspicious files — review before deploying"
        echo "   To skip: set SKIP_VT_SCAN=1 or fix flagged files"
        [ "${SKIP_VT_SCAN:-0}" = "1" ] || exit 1
    }
else
    echo "⚠️  VT_API_KEY not set — skipping VirusTotal scan"
fi

# ── 3. Атомарный swap ──
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

# ── 4. Canary monitoring (10 mins, 20 checks every 30s) ──
HEALTH_FILE=~/.local/share/bybit-ws/health.txt
MAX_CHECKS=20
CHECK_INTERVAL=30
MAX_HEALTH_AGE=300

echo "🐤 Canary monitoring: $MAX_CHECKS checks every ${CHECK_INTERVAL}s..."

do_rollback() {
    local reason="$1"
    echo "❌ $reason — ROLLBACK"
    for f in "${FILES[@]}"; do
        [ -f "$BACKUP_DIR/$f" ] && cp "$BACKUP_DIR/$f" "$TARGET/" || true
    done
    systemctl --user restart bybit-ws-async
    sleep 3
    echo "🔄 Откат завершён. Статус:"
    systemctl --user status bybit-ws-async --no-pager -l | head -5
    exit 1
}

for i in $(seq 1 $MAX_CHECKS); do
    sleep $CHECK_INTERVAL

    # Check 1: service is active
    if ! systemctl --user is-active bybit-ws-async > /dev/null 2>&1; then
        do_rollback "Check $i/$MAX_CHECKS: service NOT active"
    fi

    # Check 2: health.txt age < MAX_HEALTH_AGE seconds
    if [ ! -f "$HEALTH_FILE" ]; then
        do_rollback "Check $i/$MAX_CHECKS: health.txt NOT FOUND"
    fi

    health_age=$(($(date +%s) - $(stat -c %Y "$HEALTH_FILE")))
    if [ "$health_age" -gt "$MAX_HEALTH_AGE" ]; then
        do_rollback "Check $i/$MAX_CHECKS: health.txt age ${health_age}s > ${MAX_HEALTH_AGE}s"
    fi

    echo "  ✅ Check $i/$MAX_CHECKS: active, health_age=${health_age}s"
done

echo "✅ Canary passed — деплой успешен (PID $(systemctl --user show bybit-ws-async -p MainPID | cut -d= -f2))"
echo "   Бэкап: $BACKUP_DIR"
exit 0
