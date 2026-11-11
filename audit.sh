#!/usr/bin/env bash
# audit.sh — машинная фаза аудита bybit-ws (запускать ПЕРЕД LLM-фазой).
# HARD-гейты (exit!=0 при падении): mypy (journal-контракт) + pytest + verify.sh.
# ADVISORY (печать, не гейт): ruff на диффе + секрет-скан.
# LLM-фаза (3 адверсариальных эшелона на git diff) — скилл bybit-ws-maintenance
# → references/audit-v2-checklist.md.
#
# ruff advisory: полный прогон = 1705 предсуществующих ошибок (линт не гоняли),
# гатить нельзя до расчистки долга. Дифф с последнего аудита (fix(audit) →
# AUDIT_SINCE), включая незакоммиченное.
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
cd "$REPO"

spawn() {
  local name="$1"; shift
  "$@" >"$OUT/$name.log" 2>&1 &
  echo $! >"$OUT/$name.pid"
}

LAST_AUDIT="${AUDIT_SINCE:-$(git log --oneline | grep -iE 'fix\(audit' | head -1 | awk '{print $1}')}"
CHANGED=$(git diff --name-only "$LAST_AUDIT" -- '*.py' 2>/dev/null)

# mypy требует запуск из $HOME (дефис в имени каталога + корневой __init__.py).
spawn mypy   bash -c "cd ~ && MYPYPATH='$REPO' /usr/bin/python3 -m mypy --config-file '$REPO/mypy.ini' -p bybit_ws.journal"
spawn pytest python3 -m pytest -q
spawn verify bash -c "timeout 280 bash '$REPO/verify.sh'"

fail=0
for name in mypy pytest verify; do
  wait "$(cat "$OUT/$name.pid")" 2>/dev/null; code=$?
  if [ "$code" -eq 0 ]; then
    echo "✅ $name"
  else
    echo "❌ $name (exit $code) — хвост: $(tail -3 "$OUT/$name.log" | tr '\n' ' ')"
    fail=1
  fi
done

# ── ruff: advisory на диффе ──
echo "── ruff (advisory, дифф от $LAST_AUDIT) ──"
if [ -n "$CHANGED" ]; then
  /usr/bin/python3 -m ruff check $CHANGED 2>&1 | tail -2
else
  echo "нет изменённых .py"
fi

# ── секрет-скан: advisory ──
echo "── секрет-скан (advisory) ──"
SECRETS=$(grep -rInE \
  '(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|api_key[[:space:]]*=[[:space:]]*"[A-Za-z0-9_]{16,}")' \
  --include='*.py' --include='*.yaml' --include='*.json' --include='*.toml' \
  "$REPO" 2>/dev/null \
  | grep -vE '\.venv|test_|mock|EXAMPLE|example|getenv|environ|placeholder|YOUR_|xxxx|XXXX' \
  | head -20)
if [ -n "$SECRETS" ]; then echo "$SECRETS"; else echo "✅ совпадений нет"; fi

exit $fail
