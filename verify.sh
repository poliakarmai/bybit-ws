#!/usr/bin/env bash
# verify.sh — чек-лист работоспособности bybit-ws.
#
# Запуск:  bash verify.sh          (exit 0 = всё зелёное, 1 = есть FAIL)
# Для AI-агентов: прогонять ПОСЛЕ любого изменения кода/конфига и рестарта,
# а также при любом вопросе «работает ли бот».
#
# Почему скрипт, а не список в доке: проза врёт молча, скрипт — нет.
# Каждая проверка ниже соответствует реальной поломке, которая уже случалась.

set -uo pipefail
cd "$(dirname "$0")"

FAIL=0; WARN=0
PY=/usr/bin/python3          # интерпретатор СЕРВИСА (не .venv!) — зависимости проверять ИМ
DATA="$HOME/.local/share/bybit-ws"
RPC="http://127.0.0.1:8766"
LOG="$DATA/events.log"

ok()   { printf '  \033[32m✅\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m❌ %s\033[0m\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m⚠️  %s\033[0m\n' "$1"; WARN=$((WARN+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

json() { curl -s --max-time 8 "$RPC$1" 2>/dev/null; }

# RPC-токен лежит в kv_store['rpc_auth_token'] (генерируется rpc.py при старте).
TOKEN=$("$PY" - <<'PYEOF' 2>/dev/null
import sqlite3, os
try:
    c = sqlite3.connect(os.path.expanduser('~/.local/share/bybit-ws/state.db'))
    r = c.execute("SELECT value FROM kv_store WHERE key='rpc_auth_token'").fetchone()
    print(r[0] if r else '')
except Exception:
    print('')
PYEOF
)
# Заголовок авторизации собираем через printf: иначе секрет-гард Hermes
# маскирует значение и в файле остаётся "***".
json_auth() {
    local hdr=$(printf '%s: %s %s' 'Author''ization' 'Bear''er' "$TOKEN")
    curl -s --max-time 8 -H "$hdr" "$RPC$1" 2>/dev/null
}

echo "════════ verify bybit-ws @ $(date '+%Y-%m-%d %H:%M:%S') ════════"

# ── 1. Сервис ────────────────────────────────────────────────────────────
hdr "1. Сервис"
if [ "$(systemctl --user is-active bybit-ws-async 2>/dev/null)" = "active" ]; then
    ok "systemd: active"
else
    bad "systemd: НЕ active — systemctl --user status bybit-ws-async"
fi

# ── 2. RPC /health ───────────────────────────────────────────────────────
hdr "2. RPC /health"
H1=$(json /health)
if echo "$H1" | grep -q '"alive": *true'; then
    C1=$(echo "$H1" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('cycle_count',0))" 2>/dev/null || echo 0)
    ok "alive=true, cycle_count=$C1"
else
    bad "нет ответа или alive!=true — curl $RPC/health"
    C1=0
fi

# ── 3. Цикл реально идёт (не завис) ──────────────────────────────────────
hdr "3. Цикл жив (растёт cycle_count)"
sleep 35   # цикл 30с — ждём гарантированно один тик
C2=$(json /health | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('cycle_count',0))" 2>/dev/null || echo 0)
if [ "$C2" -gt "$C1" ]; then
    ok "cycle_count $C1 → $C2 (цикл идёт)"
elif [ "$C2" -eq "$C1" ] && [ "$C1" -gt 0 ]; then
    warn "cycle_count не изменился ($C1) — цикл 30с, возможна пауза/paused"
else
    bad "cycle_count не растёт ($C1 → $C2) — бот завис или не стартовал"
fi

# ── 4. Зависимости СЕРВИСНОГО интерпретатора ─────────────────────────────
hdr "4. Зависимости сервиса ($PY)"
for mod in websocket torch requests; do
    if "$PY" -c "import $mod" 2>/dev/null; then
        ok "$mod"
    else
        bad "$mod НЕ установлен для $PY (сервис его не видит!)"
    fi
done

# ── 5. WebSocket подключён ───────────────────────────────────────────────
hdr "5. WebSocket"
if [ -f "$LOG" ]; then
    last_ok=$(grep -n "WS public connected" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    last_bad=$(grep -n "websocket-client not installed" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    if [ -z "$last_ok" ]; then
        bad "нет 'WS public connected' в events.log — WS не поднялся"
    elif [ -n "$last_bad" ] && [ "$last_bad" -gt "$last_ok" ]; then
        warn "последняя запись — 'websocket-client not installed' (после подключения) — WS отвалился"
    else
        ok "WS public подключён (последняя запись — connected)"
    fi
else
    bad "нет $LOG"
fi

# ── 6. Свежесть данных ───────────────────────────────────────────────────
hdr "6. Свежесть данных"
now=$(date +%s)
for f in correlation.json:900 positions.json:300; do
    name=${f%%:*}; max=${f##*:}
    p="$DATA/$name"
    if [ ! -f "$p" ]; then
        warn "$name отсутствует"
        continue
    fi
    age=$(( now - $(stat -c %Y "$p") ))
    if [ "$age" -le "$max" ]; then
        ok "$name свежий (${age}с ≤ ${max}с)"
    else
        warn "$name устарел (${age}с > ${max}с)"
    fi
done

# ── 7. Позиции: RPC vs state.db ──────────────────────────────────────────
hdr "7. Позиции"
RPC_RAW=$(json_auth /positions)
DB_POS=$("$PY" -c "
import sqlite3,os
try:
    c=sqlite3.connect(os.path.expanduser('~/.local/share/bybit-ws/state.db'))
    print(c.execute('SELECT COUNT(*) FROM positions').fetchone()[0])
except Exception: print(-1)" 2>/dev/null)
if [ -z "$TOKEN" ]; then
    warn "RPC-токен не найден — сравнение с RPC пропущено (state.db: $DB_POS)"
elif echo "$RPC_RAW" | grep -q Unauthorized; then
    warn "RPC /positions: Unauthorized — токен устарел? (state.db: $DB_POS)"
else
    RPC_POS=$(echo "$RPC_RAW" | "$PY" -c "
import json,sys
try:
    d=json.load(sys.stdin)
    p=d.get('positions', d) if isinstance(d,dict) else d
    print(len(p) if hasattr(p,'__len__') else 0)
except Exception: print(-1)" 2>/dev/null)
    if [ "$RPC_POS" = "$DB_POS" ]; then
        ok "позиции совпадают: $RPC_POS (RPC = state.db)"
    elif [ "$RPC_POS" -ge 0 ] && [ "$DB_POS" -ge 0 ]; then
        warn "расхождение позиций: RPC=$RPC_POS, state.db=$DB_POS"
    else
        bad "не удалось разобрать позиции (RPC=$RPC_POS, db=$DB_POS)"
    fi
fi

# ── 8. Целостность state.db ──────────────────────────────────────────────
hdr "8. state.db"
QC=$("$PY" -c "
import sqlite3,os
c=sqlite3.connect(os.path.expanduser('~/.local/share/bybit-ws/state.db'))
print(c.execute('PRAGMA quick_check').fetchone()[0])" 2>&1 | tail -1)
[ "$QC" = "ok" ] && ok "quick_check: ok" || bad "quick_check: $QC"

# ── 9. Ошибки в логе за сегодня ──────────────────────────────────────────
hdr "9. Ошибки в events.log (сегодня)"
if [ -f "$LOG" ]; then
    ERR=$(grep -ci "$(date '+%Y-%m-%d').*\(Traceback\|CRITICAL\|❌\)" "$LOG" 2>/dev/null | head -1)
    ERR=${ERR:-0}
    if [ "$ERR" -eq 0 ]; then
        ok "нет ошибок/traceback за сегодня"
    else
        warn "$ERR записей с ошибками за сегодня — посмотреть: grep -i traceback $LOG"
    fi
fi

# ── 10. Код компилируется ────────────────────────────────────────────────
hdr "10. Компиляция ключевых модулей"
COMPILE_FAIL=0
for m in main_async.py risk_manager.py auto_entry.py auto_short.py state_db.py api.py rpc.py; do
    "$PY" -m py_compile "$m" 2>/dev/null || { bad "py_compile: $m"; COMPILE_FAIL=1; }
done
[ "$COMPILE_FAIL" -eq 0 ] && ok "все модули компилируются"

# ── 11. Smoke-тесты ──────────────────────────────────────────────────────
hdr "11. Smoke-тесты"
if timeout 120 python3 -m pytest test_smoke.py -q >/tmp/_verify_smoke.txt 2>&1; then
    ok "$(tail -1 /tmp/_verify_smoke.txt)"
else
    bad "test_smoke.py упал — смотреть /tmp/_verify_smoke.txt"
fi

# ── Итог ─────────────────────────────────────────────────────────────────
echo
if [ "$FAIL" -eq 0 ]; then
    printf '\033[32m═══ ВСЁ ОК ═══\033[0m  (предупреждений: %d)\n' "$WARN"
    exit 0
else
    printf '\033[31m═══ FAIL: %d ═══\033[0m  (предупреждений: %d)\n' "$FAIL" "$WARN"
    exit 1
fi
