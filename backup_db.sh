#!/usr/bin/env bash
#
# backup_db.sh — ежедневный бэкап SQLite SSOT (state.db) с проверкой восстановления.
#
# Источник: ~/.local/share/bybit-ws/state.db (LIVE, WAL). Снимаем снимок через
# VACUUM INTO (read-only для источника, безопасен при живом писателе — НЕ cp, НЕ .backup).
# Проверяем именно КОПИЮ: PRAGMA quick_check + совпадение числа строк trade_history.
# Ретенция: оставляем $BYBIT_BACKUP_KEEP самых свежих state_*.db, старые удаляем.
# Файлы trades_*.jsonl не трогаем — это посторонняя история.
#
set -euo pipefail

DB="${BYBIT_DB:-$HOME/.local/share/bybit-ws/state.db}"
BACKUP_DIR="${BYBIT_BACKUP_DIR:-$HOME/.local/share/bybit-ws/backups}"
KEEP="${BYBIT_BACKUP_KEEP:-7}"

log() { printf '%s backup_db: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

# ── 0. Источник должен существовать ──
if [[ ! -f "$DB" ]]; then
  echo "backup_db: ERROR: source DB not found: $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
# Наносекунды в штампе: два прогона в одну секунду не должны столкнуться —
# VACUUM INTO отказывается писать в существующий файл.
STAMP="$(date +%Y%m%d_%H%M%S_%N)"
OUT="$BACKUP_DIR/state_${STAMP}.db"

# VACUUM INTO отказывается перезаписывать существующий файл — запоминаем,
# был ли OUT до запуска, чтобы не удалить чужой/старый бэкап при коллизии имени.
existed=0
[[ -e "$OUT" ]] && existed=1

# ── 1. Снимок через VACUUM INTO ──
if ! sqlite3 "$DB" "VACUUM INTO '$OUT'"; then
  (( existed )) || rm -f "$OUT"
  echo "backup_db: ERROR: VACUUM INTO failed: $DB -> $OUT" >&2
  exit 1
fi

# ── 2. Проверяем КОПИЮ, не источник ──
check="$(sqlite3 "$OUT" 'PRAGMA quick_check;' || true)"
if [[ "$check" != "ok" ]]; then
  rm -f "$OUT"
  echo "backup_db: ERROR: quick_check failed on copy: '${check:-<empty>}'" >&2
  exit 1
fi

# ── 3. Копия не пустая/битая: совпадение числа строк trade_history ──
src_count="$(sqlite3 "$DB" 'SELECT count(*) FROM trade_history;' || true)"
cpy_count="$(sqlite3 "$OUT" 'SELECT count(*) FROM trade_history;' || true)"
if [[ "$src_count" != "$cpy_count" ]]; then
  rm -f "$OUT"
  echo "backup_db: ERROR: trade_history row count mismatch: source=$src_count copy=$cpy_count" >&2
  exit 1
fi

size="$(du -h "$OUT" | cut -f1)"

# ── 4. Ретенция: оставляем KEEP самых свежих state_*.db (по mtime) ──
pruned=0
if compgen -G "$BACKUP_DIR/state_*.db" > /dev/null; then
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    rm -f "$f"
    pruned=$((pruned + 1))
  done < <(ls -1t "$BACKUP_DIR"/state_*.db | tail -n +$((KEEP + 1)))
fi

log "backup ok: file=$OUT size=$size quick_check=ok rows=$cpy_count pruned=$pruned"
exit 0
