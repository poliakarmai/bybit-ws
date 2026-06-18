# Troubleshooting FAQ — bybit-ws

> Расширенное руководство по диагностике и исправлению проблем.  
> Основано на реальных ошибках из кода и `events.log`.

---

## Содержание

1. [Сервис не запускается](#1-сервис-не-запускается)
2. [RPC не отвечает](#2-rpc-не-отвечает)
3. [Ордера не выставляются](#3-ордера-не-выставляются)
4. [Позиции не открываются](#4-позиции-не-открываются)
5. [Медленные циклы](#5-медленные-циклы)
6. [Watchdog убивает](#6-watchdog-убивает)
7. [Ошибки API](#7-ошибки-api)
8. [Проблемы с конфигом](#8-проблемы-с-конфигом)
9. [Проблемы с синхронизацией](#9-проблемы-с-синхронизацией)
10. [SQLite-проблемы](#10-sqlite-проблемы)
11. [Telegram-бот не отвечает](#11-telegram-бот-не-отвечает)
12. [MCP-сервер не работает](#12-mcp-сервер-не-работает)
13. [Память растёт](#13-память-растёт)

---

## 1. Сервис не запускается

### Симптомы
- `systemctl --user status bybit-ws` показывает `failed` или `inactive`
- `journalctl --user -u bybit-ws` содержит traceback
- Запуск `python -m bybit-ws` падает с ошибкой

### Причина 1: Нет API-ключей

**Симптом:** в логах `⚠️ api: credentials not loaded — API calls will fail` или `⚠️ api: cannot read credentials`

**Причина:** Файл `~/.config/bybit-cli/config` отсутствует или не содержит `BYBIT_API_KEY` / `BYBIT_API_SECRET`.

**Решение:**
```bash
# Проверить наличие ключей
cat ~/.config/bybit-cli/config | grep BYBIT_API

# Если нет — создать:
mkdir -p ~/.config/bybit-cli
cat > ~/.config/bybit-cli/config << 'EOF'
BYBIT_API_KEY=your_key_here
BYBIT_API_SECRET=your_secret_here
EOF
chmod 600 ~/.config/bybit-cli/config
```

### Причина 2: Не установлен `bybit` CLI

**Симптом:** в логах `bybit exception: [Errno 2] No such file or directory: 'bybit'` (массово, каждый цикл)

**Причина:** Бинарник `bybit` CLI, используемый `health.py` для `check_funding_flip`, `check_daily_drawdown`, `check_funding_pump`, отсутствует.

**Решение:**
```bash
# Проверить
ls -la ~/.local/bin/bybit

# Установить bybit-cli
pip install bybit-cli
# или
cargo install bybit-cli
```

### Причина 3: Нет `config.yaml`

**Симптом:** При запуске сообщение `📝 Example config written to .../config.example.yaml`

**Причина:** Сервис использует дефолтные настройки, но без API-ключей работать не будет.

**Решение:**
```bash
cp ~/.config/bybit-ws/config.example.yaml ~/.config/bybit-ws/config.yaml
# Отредактировать: vim ~/.config/bybit-ws/config.yaml
# Проверить валидность YAML:
python3 -c "import yaml; yaml.safe_load(open('$HOME/.config/bybit-ws/config.yaml'))"
```

### Причина 4: Конфликт портов (RPC)

**Симптом:** `⚠️ RPC-сервер не запустился: [Errno 98] Address already in use`

**Причина:** Порт 8766 занят другим процессом (возможно, второй экземпляр bybit-ws).

**Решение:**
```bash
# Найти процесс на порту
ss -tlnp | grep 8766
# Убить старый процесс
sudo kill $(lsof -ti:8766)
# Или сменить порт в config.yaml: rpc.port: 8767
```

### Причина 5: Повреждённое venv-окружение

**Симптом:** `ModuleNotFoundError: No module named 'bybit_ws'` при запуске из systemd.

**Причина:** systemd указывает на `~/.local/lib/bybit_ws/`, но модули не синхронизированы.

**Решение:**
```bash
# Синхронизировать установленную копию
cd ~/bybit-ws
cp *.py ~/.local/lib/bybit_ws/
cp -r bybit_ws/ ~/.local/lib/bybit_ws/
# Проверить
ls ~/.local/lib/bybit_ws/__init__.py
```

### Причина 6: `ImportError` из-за отсутствующих зависимостей

**Симптом:** `ModuleNotFoundError: No module named 'yaml'` или `No module named 'requests'`.

**Решение:**
```bash
cd ~/bybit-ws
source .venv/bin/activate
pip install -r requirements.txt
# или вручную:
pip install pyyaml requests
```

---

## 2. RPC не отвечает

### Симптомы
- `curl http://127.0.0.1:8766/health` → `Connection refused`
- MCP-инструменты возвращают `Error: no data`
- Дашборд не загружается

### Причина 1: Сервис не запущен

**Симптом:** `curl: (7) Failed to connect to 127.0.0.1 port 8766: Connection refused`

**Решение:**
```bash
systemctl --user status bybit-ws
# Если inactive:
systemctl --user start bybit-ws
# Ждать 30 секунд — RPC стартует после первого цикла
sleep 30
curl http://127.0.0.1:8766/health
```

### Причина 2: RPC запущен, но на другом порту

**Симптом:** `Connection refused` на 8766.

**Причина:** В `config.yaml` указан другой порт в `rpc.port`.

**Решение:**
```bash
# Проверить конфиг
grep -A2 'rpc:' ~/.config/bybit-ws/config.yaml
# Узнать порт через API:
curl http://127.0.0.1:8766/rpc/paths 2>/dev/null | python3 -m json.tool
# Или использовать правильный порт:
curl http://127.0.0.1:<правильный_порт>/health
```

### Причина 3: RPC bind = `127.0.0.1`, а клиент на внешнем IP

**Симптом:** `Connection refused` с другого хоста, но `curl localhost:8766` работает.

**Причина:** По умолчанию `rpc.bind: "127.0.0.1"` — слушает только localhost.

**Решение:**
```yaml
# В ~/.config/bybit-ws/config.yaml:
rpc:
  bind: "0.0.0.0"   # слушать на всех интерфейсах
```
```bash
systemctl --user restart bybit-ws
```

### Причина 4: Ошибка авторизации (401)

**Симптом:** `{"error": "Unauthorized", "error_code": "unauthorized"}`

**Причина:** Отсутствует или неверный `Authorization: Bearer <token>`.

**Решение:**
```bash
# Получить токен из SQLite
python3 -c "
import sqlite3, json
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
row = conn.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()
print(row[0] if row else 'NOT FOUND')
"

# Использовать:
TOKEN=$(python3 -c "import sqlite3; c=sqlite3.connect('$HOME/.local/share/bybit-ws/state.db'); print(c.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])")
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/positions
```

### Причина 5: Rate limit (429)

**Симптом:** `{"error": "Rate limit exceeded", "error_code": "rate_limit"}`

**Причина:** Более 60 запросов в минуту с одного IP.

**Решение:**
```bash
# Подождать 60 секунд
# Или увеличить лимит в конфиге:
# rpc.rate_limit_per_min: 120
systemctl --user restart bybit-ws
```

### Причина 6: Health = `stale`

**Симптом:** `GET /health` возвращает `{"status": "stale", "alive": false}`

**Причина:** Главный цикл не обновлял `health.txt` более 180 секунд — монитор завис.

**Решение:**
```bash
# Проверить время последнего цикла
cat ~/.local/share/bybit-ws/health.txt
date +%s  # сравнить разницу

# Рестарт
systemctl --user restart bybit-ws

# Проверить причину зависания:
journalctl --user -u bybit-ws --since "5 min ago" | tail -30
```

---

## 3. Ордера не выставляются

### Симптомы
- В логах нет `📌 **Лимитка сработала!**`
- Позиции открываются без SL/TP
- Ордер создаётся, но мгновенно отменяется

### Причина 1: Недостаточно маржи

**Симптом:** `⚠️ Auto-SHORT ...: ошибка — current position is zero, cannot fix reduce-only order qty`  
Или: `ab not enough for new order` (retCode 10001)

**Причина:** На счету недостаточно USDT для новой позиции.

**Решение:**
```bash
# Проверить баланс
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/balance

# Проверить суммарную маржу
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/risk
# Смотрим total_margin vs max_total_margin
```

### Причина 2: Qty ниже минимального лота

**Симптом:** `retCode 20001 — "order quantity or price too low"`

**Причина:** Количество меньше `lotSizeFilter.qtyStep` для данного символа.

**Решение:**
```bash
# Проверить минимальный лот
python3 -c "
from bybit_ws.api import bybit
r = bybit('GET', '/v5/market/instruments-info?category=linear&symbol=UNIUSDT')
info = r['result']['list'][0]
print('qtyStep:', info['lotSizeFilter']['qtyStep'])
print('minQty:', info['lotSizeFilter']['minOrderQty'])
"
# Увеличить qty до ближайшего шага
```

### Причина 3: Hedge mode — неверный positionIdx

**Симптом:** `retCode 10001 — "position idx not match position mode"`

**Причина:** Для хедж-режима Bybit требует positionIdx=1 для SHORT, но ордер отправлен с idx=0.

**Решение:** Обрабатывается автоматически в RPC `/enter` (retry с idx=1). При ручном вызове API:
```bash
# Сначала получить правильный idx:
curl -X GET ".../v5/position/list?category=linear&symbol=SYMUSDT" | jq '.result.list[0].positionIdx'
```

### Причина 4: Ручная позиция (manual)

**Симптом:** В логах `🔒 SYMUSDT: SL $X.XXXX > entry $Y.YYYY — ручная фиксация, не трогаем`

**Причина:** Позиция помечена как ручная (`manual_positions.is_manual_position()`), авто-SL/TP пропускаются.

**Решение:**
```bash
# Проверить pumps.json на наличие флага manual
cat ~/.local/share/bybit-ws/pumps.json | python3 -m json.tool | grep -A5 '"manual": true'

# Снять ручной флаг (убрать символ из pumps.json):
python3 -c "
import json
with open('$HOME/.local/share/bybit-ws/pumps.json') as f:
    p = json.load(f)
if 'SYMUSDT' in p and p['SYMUSDT'].get('manual'):
    del p['SYMUSDT']['manual']
with open('$HOME/.local/share/bybit-ws/pumps.json', 'w') as f:
    json.dump(p, f)
"
```

### Причина 5: Позиция в прибыли — SL не ставится

**Симптом:** Позиция без SL, но в логах нет попыток поставить.

**Причина:** `auto_sl.check_and_fix_sl()` пропускает позиции где `mark > entry` (LONG) или `mark < entry` (SHORT) — ждёт TP.

**Решение:** Это нормальное поведение. SL будет поставлен автоматически, когда:
- Прибыль станет >10% → безубыток через `check_breakeven_sl()`
- Цена вернётся к входу → авто-SL

---

## 4. Позиции не открываются

### Симптомы
- В логах `🛑 Авто-вход заблокирован: ...`
- `🛑 DCA заблокирован: risk-лимит`
- `🛑 X10 блок: ...`
- Сканер находит сигналы, но входы не происходят

### Причина 1: Risk-лимиты превышены

**Симптом:** `🛑 Авто-вход заблокирован: risk-лимит (max_daily_loss / max_total_margin)`

**Причина:** Сработал один из лимитов:
- `max_daily_loss` (по умолчанию $50) — дневной PnL < −$50
- `max_total_margin` (по умолчанию $500) — суммарная маржа превышена
- `max_long_positions` (по умолчанию 12) — слишком много LONG

**Решение:**
```bash
# Проверить текущие лимиты
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/risk

# Сбросить дневной PnL (осторожно!)
python3 -c "
import json
with open('$HOME/.local/share/bybit-ws/metrics.json') as f:
    m = json.load(f)
# Сбросить сегодняшний PnL:
import datetime
today = datetime.date.today().isoformat()
for k in list(m.keys()):
    if k.startswith(today):
        if 'pnl_total' in m[k]:
            m[k]['pnl_total'] = 0
with open('$HOME/.local/share/bybit-ws/metrics.json', 'w') as f:
    json.dump(m, f)
"

# Или увеличить лимиты в config.yaml:
# risk.max_daily_loss: 100
# risk.max_total_margin: 1000
```

### Причина 2: Корреляция блокирует входы

**Симптом:** `🛑 Корреляция 86% LONG — авто-вход заблокирован`

**Причина:** Более 80% открытых позиций в LONG — вход новых LONG заблокирован для снижения концентрационного риска.

**Решение:**
```bash
# Проверить корреляцию
cat ~/.local/share/bybit-ws/correlation.json | python3 -m json.tool
# Открыть SHORT-позиции для балансировки, или закрыть часть LONG
# SHORT-входы продолжают работать даже при блокировке LONG
```

### Причина 3: Banned символ

**Симптом:** Сигналы на символ есть, но вход не происходит.

**Причина:** Символ в списке `risk.banned_symbols` в `config.yaml`.

**Решение:**
```bash
# Проверить список banned
python3 -c "
from bybit_ws.config import Config
print(Config().risk.banned_symbols)
"
# Убрать символ из banned_symbols в config.yaml
```

### Причина 4: Превышен лимит позиций в секторе

**Симптом:** Сигнал на L1-монету есть, но вход блокирован.

**Причина:** `risk.max_per_sector: 3` — не более 3 позиций в одном секторе.

**Решение:**
```bash
# Проверить распределение по секторам
python3 -c "
from bybit_ws.config import Config
sectors = Config().risk.sectors
for sector, syms in sectors.items():
    print(f'{sector}: {syms}')
"
# Увеличить лимит: risk.max_per_sector: 5
```

### Причина 5: SL re-entry лесенка ждёт

**Симптом:** `⏳ SL re-entry ALLOUSDT: не удалось поставить лимитки, ждём`

**Причина:** После SL срабатывания система пытается перезайти по лесенке, но цена не достигла нужного уровня.

**Решение:** Это нормально — лимитки GTC ждут падения цены на N%. Если не хотите ждать:
```bash
# Отменить лимитки вручную через RPC:
curl -X POST http://127.0.0.1:8766/close -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "ALLOUSDT"}'
```

### Причина 6: Пауза (paused)

**Симптом:** Сигналы игнорируются, новых ордеров нет.

**Причина:** Кто-то вызвал `POST /pause` — торговля приостановлена.

**Решение:**
```bash
curl -X POST http://127.0.0.1:8766/resume \
  -H "Authorization: Bearer $TOKEN"
```

---

## 5. Медленные циклы

### Симптомы
- В логах `⚠️ Цикл 134.1s — превышен порог 20s`
- `⏭️ Цикл перегружен (95с) — тяжёлые проверки пропущены`
- RPC `/health` показывает `cycle_duration > 60`
- Prometheus `bybit_ws_cycle_duration_seconds > 30`

### Причина 1: Слишком много API-запросов в корреляции

**Симптом:** Циклы замедляются когда >5 позиций открыто.

**Причина:** `check_correlation()` делает N×(N−1)/2 запросов klines — при 7 позициях это 21 запрос.

**Решение:**
```bash
# Уменьшить watchlist:
# watchlist.top_n: 30  (вместо 50)

# Увеличить интервал тяжёлых проверок:
# monitor.heavy_cycle: 15  (вместо 10 — каждые 7.5 мин вместо 5)
```

### Причина 2: Проблемы с сетью / DNS

**Симптом:** Циклы стабильно >60s, в логах `bybit timeout after 15s` или `bybit connection error`.

**Решение:**
```bash
# Проверить доступность API
curl -s --connect-timeout 5 https://api.bytick.com/v5/market/time
# При проблемах с DNS:
dig api.bytick.com
# Попробовать альтернативный URL в config.yaml:
# api.base_url: "https://api.bybit.com"
```

### Причина 3: Большой events.log

**Симптом:** Циклы постепенно замедляются со временем.

**Причина:** Файл `events.log` растёт (>8 MB в проде), `log_event()` пишет синхронно на каждом шаге.

**Решение:**
```bash
# Проверить размер
ls -lh ~/.local/share/bybit-ws/events.log

# Ротация вручную:
mv ~/.local/share/bybit-ws/events.log ~/.local/share/bybit-ws/events.log.old
systemctl --user restart bybit-ws

# Или настроить авто-ротацию:
# logging.max_size_mb: 20  (вместо 50)
# logging.max_files: 3     (вместо 7)
```

### Причина 4: `check_funding_flip()` делает N subprocess-вызовов

**Симптом:** Каждый тяжёлый цикл дольше 30s.

**Причина:** `health.py:check_funding_flip()` вызывает `bybit ticker <sym>` через subprocess для каждого символа в watchlist (50+ символов).

**Решение:**
```bash
# Уменьшить watchlist
# Или увеличить heavy_cycle чтобы реже вызывать
```

---

## 6. Watchdog убивает

### Симптомы
- `🚨 Watchdog: главный цикл завис (190с) — аварийный выход`
- systemd перезапускает сервис (`systemctl --user status bybit-ws` показывает частые рестарты)
- `journalctl --user -u bybit-ws` показывает циклы перезапусков

### Причина 1: 404-флуд от Bybit API

**Симптом:** В логах сотни строк `bybit 404 (endpoint not found, skipping): HTTP 404 GET /v5/position/info?...`

**Причина:** Используется устаревший эндпоинт `/v5/position/info`, который больше не поддерживается Bybit. Каждый запрос мгновенно фейлится, но N запросов × 15s timeout накапливаются.

**Решение:**
```bash
# Найти источник 404 в коде
grep -r "position/info" ~/bybit-ws/ ~/.local/lib/bybit_ws/
# Заменить на /v5/position/list?category=linear&symbol=SYMUSDT
# Проверить, не используется ли в MCP-сервере или старых скриптах
```

### Причина 2: Сетевой блэкаут

**Симптом:** Внезапный watchdog kill, в логах `bybit connection error` перед смертью.

**Причина:** Сеть пропала — все API-запросы висят по 15 секунд, суммарно >180s.

**Решение:**
```bash
# Проверить сеть:
ping -c 3 api.bytick.com
# Настроить авто-восстановление (systemd):
systemctl --user cat bybit-ws
# Restart=always уже должен быть в unit-файле
```

### Причина 3: Таймаут в `_timed_call()`

**Симптом:** `⏱️ check_correlation: таймаут` и другие функции в логах.

**Причина:** Каждая проверка в `_run_heavy_cycle()` имеет таймаут 25 секунд — если несколько функций зависают, цикл уходит за 180s.

**Решение:**
```bash
# Проверить конкретную функцию:
grep "⏱️.*таймаут" ~/.local/share/bybit-ws/events.log | sort | uniq -c | sort -rn

# Увеличить watchdog_seconds:
# monitor.watchdog_seconds: 300  (вместо 180)
```

### Причина 4: Deadlock в SQLite

**Симптом:** Watchdog убивает без явных ошибок в логах.

**Причина:** Два потока пытаются писать в `state.db` одновременно (WAL обычно спасает, но `busy_timeout=5000` может не хватить).

**Решение:**
```bash
# Проверить state.db на целостность:
sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check;"
# При проблемах — восстановить из бэкапа:
ls ~/.local/share/bybit-ws/backups/
cp ~/.local/share/bybit-ws/backups/state_$(date +%Y%m%d)*.db ~/.local/share/bybit-ws/state.db
```

---

## 7. Ошибки API

### 7.1 HTTP 404 — Endpoint Not Found

**Симптом:** `bybit 404 (endpoint not found, skipping): HTTP 404 GET /v5/...`

**Причины:**
- Используется удалённый/переименованный эндпоинт
- Неверный путь API (например, `/v5/position/info` вместо `/v5/position/list`)

**Решение:**
```bash
# Проверить актуальные эндпоинты:
# https://bybit-exchange.github.io/docs/v5/category
grep -r "position/info\|funding-history\|kline_v2" ~/bybit-ws/ ~/.local/lib/bybit_ws/
```

### 7.2 HTTP 429 — Rate Limit

**Симптом:** `bybit 429 rate-limit, backoff 1/3 in 1s`

**Причина:** Превышен лимит запросов к Bybit API (обычно 50 запросов/сек).

**Решение:**
```bash
# Встроенный retry с exponential backoff (1s → 2s → 4s → 8s → 16s)
# Не требует действий — система сама восстановится
# Если повторяется часто — уменьшить частоту запросов:
# monitor.cycle_seconds: 45  (вместо 30)
```

### 7.3 Таймаут

**Симптом:** `bybit timeout after 15s: GET /v5/market/kline?...`

**Причина:** Bybit API не отвечает за 15 секунд. Возможные причины:
- Проблемы на стороне Bybit
- Сетевые проблемы
- DNS не резолвится

**Решение:**
```bash
# Проверить доступность
time curl -s https://api.bytick.com/v5/market/time

# Увеличить таймаут в config.yaml:
# api.timeout: 30
# api.retry_backoff: [2, 5, 15]
```

### 7.4 Ошибка авторизации (retCode 10004)

**Симптом:** `retCode 10004 — "invalid api key"` или `"signature not match"`

**Причина:**
- Просроченный/неверный API-ключ
- Системные часы рассинхронизированы (timestamp в HMAC подписи не совпадает с серверным)

**Решение:**
```bash
# Проверить системное время
timedatectl status
# Синхронизировать если нужно:
sudo timedatectl set-ntp true

# Проверить ключи в config.yaml:
grep -v '^#' ~/.config/bybit-ws/config.yaml | grep -E 'key|secret'
# Или в ~/.config/bybit-cli/config:
grep BYBIT_API ~/.config/bybit-cli/config
```

### 7.5 Position IDX mismatch (retCode 10001)

**Симптом:** `retCode 10001 — "position idx not match position mode"`

**Причина:** Аккаунт в hedge mode, но передан positionIdx=0 вместо 1.

**Решение:** Обрабатывается автоматически в RPC `/enter` и `api.py`. При ручных запросах:
```bash
# Узнать режим:
python3 -c "
from bybit_ws.api import bybit
r = bybit('GET', '/v5/account/info')
print(r['result']['positionMode'])
"
```

---

## 8. Проблемы с конфигом

### Причина 1: Невалидный YAML

**Симптом:** `yaml.scanner.ScannerError` или `yaml.parser.ParserError` при запуске.

**Решение:**
```bash
# Проверить синтаксис
python3 -c "import yaml; yaml.safe_load(open('$HOME/.config/bybit-ws/config.yaml'))"

# Типичные ошибки:
# - Табы вместо пробелов (YAML требует пробелы)
# - Несовпадение отступов
# - Спецсимволы без кавычек (например, : в значении)
```

### Причина 2: Переменные окружения не подставлены

**Симптом:** В логах значения вида `${BYBIT_API_KEY}` вместо реальных ключей.

**Причина:** Переменная окружения не задана, а fallback вида `${VAR:-default}` не используется.

**Решение:**
```bash
# Проверить что переменные заданы
echo $BYBIT_API_KEY
echo $BYBIT_API_SECRET
echo $RPC_TOKEN

# Задать в ~/.bashrc или ~/.zshrc:
export BYBIT_API_KEY="your_key"
export BYBIT_API_SECRET="your_secret"

# Или прописать ключи напрямую в config.yaml:
# api.key: "your_actual_key"  (вместо ${BYBIT_API_KEY})
```

### Причина 3: Tier mismatch — символ не входит в tiers

**Симптом:** Символ не получает правильный SL (дефолтный +7% вместо +5%).

**Причина:** Символ не добавлен в `tiers.A`, `tiers.B`, `tiers.S` или `tiers.one_way`.

**Решение:**
```yaml
# В config.yaml добавить символ в соответствующий тир:
tiers:
  A:
    - SOLUSDT
    - NEWSYMUSDT   # ← добавить сюда
```

### Причина 4: Deep-merge перезаписывает дефолты

**Симптом:** После добавления одной секции в config.yaml пропали дефолты в других секциях.

**Причина:** `_deep_merge()` рекурсивно мержит — если в пользовательском конфиге есть пустая секция, она не перезапишет дефолты. Но если секция отсутствует — дефолты сохраняются.

**Решение:**
```bash
# Посмотреть итоговый конфиг (без секретов):
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/config | python3 -m json.tool
```

---

## 9. Проблемы с синхронизацией

### Симптомы
- После редактирования `.py` файлов изменения не применяются
- `ModuleNotFoundError` после обновления
- Разные версии кода в `~/bybit-ws/` и `~/.local/lib/bybit_ws/`

### Причина 1: Редактирование не того файла

**Объяснение:** systemd-сервис запускает код из `~/.local/lib/bybit_ws/`, а не из `~/bybit-ws/`. Редактирование файлов в репо не даёт эффекта без синхронизации.

**Решение:**
```bash
# Синхронизировать все .py файлы:
cd ~/bybit-ws
cp *.py ~/.local/lib/bybit_ws/
cp -r bybit_ws/ ~/.local/lib/bybit_ws/
cp -r risk/ ~/.local/lib/bybit_ws/
cp -r web/ ~/.local/lib/bybit_ws/

# Перезапустить сервис:
systemctl --user restart bybit-ws

# Проверить версию:
curl http://127.0.0.1:8766/rpc/paths | python3 -c "import json,sys; print(json.load(sys.stdin)['sync_command'])"
```

### Причина 2: `pip install -e` создал симлинки

**Симптом:** `pip install -e .` указывает на старую версию или битые симлинки.

**Решение:**
```bash
# Найти установку:
pip show bybit-ws

# Если используется -e (editable):
pip uninstall bybit-ws
pip install -e ~/bybit-ws

# Проверить симлинки:
ls -la ~/.local/lib/bybit_ws/
```

### Причина 3: Забыли перезапустить сервис

**Симптом:** Код синхронизирован, но изменения не применяются.

**Решение:**
```bash
systemctl --user restart bybit-ws
# Проверить что перезапустился:
systemctl --user status bybit-ws
```

---

## 10. SQLite-проблемы

### Причина 1: WAL-файлы не закрываются

**Симптом:** `state.db-wal` и `state.db-shm` остаются после остановки сервиса.

**Причина:** Нормальное поведение WAL-режима — файлы содержат незафиксированные транзакции.

**Решение:**
```bash
# Проверить размер WAL-файлов
ls -lh ~/.local/share/bybit-ws/state.db*

# Принудительный checkpoint:
sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA wal_checkpoint(TRUNCATE);"

# Если WAL-файл огромный (>100 MB):
systemctl --user stop bybit-ws
sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA wal_checkpoint(TRUNCATE);"
systemctl --user start bybit-ws
```

### Причина 2: Database is locked

**Симптом:** `sqlite3.OperationalError: database is locked`

**Причина:** Другой процесс (или поток) держит блокировку дольше `busy_timeout=5000` (5 секунд).

**Решение:**
```bash
# Найти процессы, использующие state.db:
lsof ~/.local/share/bybit-ws/state.db

# Убить зависшие процессы (осторожно — остановит монитор):
sudo kill <PID>

# Проверить целостность:
sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check;"
```

### Причина 3: Повреждённая база (corrupted)

**Симптом:** `sqlite3.DatabaseError: database disk image is malformed`

**Причина:** Жёсткая остановка (kill -9) или сбой диска во время записи.

**Решение:**
```bash
# Попытаться восстановить:
cp ~/.local/share/bybit-ws/state.db ~/.local/share/bybit-ws/state.db.bak
sqlite3 ~/.local/share/bybit-ws/state.db ".recover" | sqlite3 ~/.local/share/bybit-ws/state_recovered.db

# Если не помогло — восстановить из бэкапа:
ls -lt ~/.local/share/bybit-ws/backups/
cp ~/.local/share/bybit-ws/backups/state_<latest>.db ~/.local/share/bybit-ws/state.db
systemctl --user restart bybit-ws
```

### Причина 4: VACUUM для сжатия

**Симптом:** `state.db` растёт (>10 MB) несмотря на небольшое количество данных.

**Решение:**
```bash
# Оптимизировать:
sqlite3 ~/.local/share/bybit-ws/state.db "VACUUM;"
# Размер должен уменьшиться
ls -lh ~/.local/share/bybit-ws/state.db*
```

---

## 11. Telegram-бот не отвечает

### Симптомы
- Алерты не приходят в Telegram
- В логах `⚠️ Telegram send failed (rc=1): ...`
- `⚠️ Telegram send failed: hermes binary not found`
- `⚠️ Telegram send timeout (15s)`
- `🔇 Дедупликация [STOP]: пропущен алерт`

### Причина 1: `hermes` бинарник не найден

**Симптом:** `⚠️ Telegram send failed: hermes binary not found`

**Причина:** Файл `~/.local/bin/hermes` отсутствует или неисполняемый.

**Решение:**
```bash
# Проверить
ls -la ~/.local/bin/hermes
which hermes

# Если нет — установить hermes CLI:
# (зависит от способа установки)
cargo install hermes-cli
```

### Причина 2: Telegram не настроен

**Симптом:** Тишина, алерты пишутся в лог, но не отправляются.

**Причина:** `alerts.telegram_enabled: false` в config.yaml (дефолт).

**Решение:**
```yaml
# В ~/.config/bybit-ws/config.yaml:
alerts:
  telegram_enabled: true
```
```bash
systemctl --user restart bybit-ws
```

### Причина 3: Telegram API rate limit / timeout

**Симптом:** `⚠️ Telegram send timeout (15s)`

**Решение:**
```bash
# Проверить доступность Telegram API:
curl -s --connect-timeout 5 https://api.telegram.org

# Увеличить таймаут (в alerts.py send_telegram_alert timeout=15):
# grep -n "timeout=15" ~/.local/lib/bybit_ws/alerts.py
```

### Причина 4: Дедупликация глушит алерты

**Симптом:** `🔇 Дедупликация [STOP]: пропущен алерт`

**Причина:** Алерт того же типа на тот же символ уже был отправлен в течение cooldown-периода (STOP: 10 мин, TP: 5 мин, ENTRY: 5 мин).

**Решение:**
```bash
# Очистить кеш дедупликации:
rm ~/.local/share/bybit-ws/last_alerts.json
# SQLite-уровень:
sqlite3 ~/.local/share/bybit-ws/state.db "DELETE FROM alert_dedup;"
systemctl --user restart bybit-ws
```

---

## 12. MCP-сервер не работает

### Симптомы
- Инструменты MCP (scan_market, get_positions, etc.) возвращают ошибки или пустые данные
- `Error: no data` / `Metrics unavailable` / `Positions: Error: ...`
- MCP-сервер не запускается (`ModuleNotFoundError: No module named 'mcp'`)

### Причина 1: bybit-ws не запущен

**Симптом:** MCP отвечает, но все инструменты возвращают `Error: ...`

**Причина:** MCP-сервер (`~/.local/bin/bybit-mcp-server.py`) общается с RPC (порт 8766). Если RPC не запущен — curl возвращает ошибку.

**Решение:**
```bash
# Проверить RPC
curl http://127.0.0.1:8766/health
# Если Connection refused:
systemctl --user start bybit-ws
```

### Причина 2: RPC-токен не совпадает

**Симптом:** MCP инструменты возвращают `{"error": "Unauthorized"}`

**Причина:** MCP-сервер читает токен из `state.db/kv_store/rpc_auth_token`, а RPC ожидает другой токен (например, из `config.yaml`).

**Решение:**
```bash
# Проверить токены:
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
print('MCP token:', conn.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])
"

# Сбросить токен (генерирует новый UUID):
curl -X POST http://127.0.0.1:8766/reset-token \
  -H "Authorization: Bearer $TOKEN"
```

### Причина 3: `mcp` модуль не установлен

**Симптом:** `ModuleNotFoundError: No module named 'mcp'`

**Решение:**
```bash
pip install mcp
# или в venv:
cd ~/bybit-ws && source .venv/bin/activate && pip install mcp
```

### Причина 4: curl таймаут в MCP

**Симптом:** Инструменты зависают на 10+ секунд и возвращают `{"error": "..."}`.

**Причина:** `_rpc()` и `_rpc_post()` имеют `--max-time 10` (GET) и `--max-time 15` (POST) для curl. Если RPC обрабатывает запрос >10s — таймаут.

**Решение:**
```bash
# Проверить загрузку RPC:
curl -w "\n%{time_total}s\n" -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8766/rpc/all | tail -1

# Увеличить таймаут в bybit-mcp-server.py:
# --max-time 30 (вместо 10)
```

### Причина 5: gridSignal scanner не найден

**Симптом:** `scan_market` возвращает `[{"error": "..."}]` или пустой список.

**Причина:** `~/.local/bin/gridsignal_scanner.py` отсутствует или возвращает ошибку.

**Решение:**
```bash
ls -la ~/.local/bin/gridsignal_scanner.py
python3 ~/.local/bin/gridsignal_scanner.py --mode long --tf D --limit 3
```

---

## 13. Память растёт (memory leak диагностика)

### Симптомы
- RSS монитора растёт со временем (норма: ~23.5 MB)
- `htop` показывает постоянный рост памяти
- После недели аптайма память >200 MB

### Причина 1: `_SL_DEDUP` словарь не очищается

**Симптом:** Постепенный рост памяти (несколько KB в день).

**Причина:** Словарь `_SL_DEDUP` в `main.py` хранит ключи symbol→timestamp для SL-алертов. Очистка происходит только когда словарь >1000 записей или записи старше 24ч.

**Решение:** Уже реализована авто-очистка в `main_loop()`:
```python
_SL_DEDUP.clear() if len(_SL_DEDUP) > 1000 else [
    _SL_DEDUP.pop(sym, None) for sym in list(_SL_DEDUP)
    if now_ts - _SL_DEDUP.get(sym, 0) > 86400
]
```
Если память всё равно растёт — проверить что очистка выполняется (искать `SL_DEDUP` в логах).

### Причина 2: `_rate_limit_store` в rpc.py

**Симптом:** Память растёт с каждым новым IP-клиентом.

**Причина:** `defaultdict` хранит запись для каждого уникального IP, который обращался к RPC. В норме это единицы записей, но при DDoS или частых запросах с разных IP может расти.

**Решение:**
```bash
# Очистка требует рестарта:
systemctl --user restart bybit-ws
# Для постоянного решения — добавить TTL-очистку в rpc.py
```

### Причина 3: `ALERTS` список в `__init__.py`

**Симптом:** При большом количестве алертов между очистками память растёт.

**Причина:** Глобальный список `ALERTS` накапливает все алерты между вызовами `get_alerts()` (каждые 2 цикла = 60 секунд). В спокойном режиме это единицы записей, но при шторме может быть сотни.

**Решение:** Норма — `get_alerts()` очищает список каждые 60 секунд. При аномальном росте:
```bash
# Проверить размер alerts.log
wc -l ~/.local/share/bybit-ws/alerts.log
```

### Причина 4: `DEPOSIT_CACHE` в position_sizing.py

**Симптом:** Незначительно.

**Причина:** Кеш депозита — фиксированного размера, не должен расти.

**Решение:** Не требует действий.

### Причина 5: Циклические ссылки в модулях

**Симптом:** Память растёт даже при отсутствии алертов.

**Причина:** Возможные циклические импорты между модулями (Python GC обычно справляется).

**Решение:**
```bash
# Проверить память процесса:
ps -o pid,rss,comm -p $(systemctl --user show -p MainPID bybit-ws | cut -d= -f2)

# Профилировать:
python3 -c "
import bybit_ws.main
import tracemalloc
tracemalloc.start()
# ... run one cycle ...
snapshot = tracemalloc.take_snapshot()
for stat in snapshot.statistics('lineno')[:10]:
    print(stat)
"
```

### Причина 6: `trades.jsonl` и `events.log` вне процесса

**Симптом:** Память процесса в норме, но диск заполняется.

**Причина:** `events.log` (8+ MB) и `trades.jsonl` неограниченно растут.

**Решение:**
```bash
# Проверить размеры
du -sh ~/.local/share/bybit-ws/events.log ~/.local/share/bybit-ws/trades.jsonl

# Ротация events.log (автоматически при >50 MB, но можно раньше):
mv ~/.local/share/bybit-ws/events.log ~/.local/share/bybit-ws/events.log.old

# Настроить лимиты в config.yaml:
# logging.max_size_mb: 20
# logging.trades_max_size_mb: 50
```

---

## Быстрая диагностика (шпаргалка)

```bash
# ═══ Статус сервиса ═══
systemctl --user status bybit-ws

# ═══ RPC health ═══
curl -s http://127.0.0.1:8766/health | python3 -m json.tool

# ═══ Последние 30 строк лога ═══
tail -30 ~/.local/share/bybit-ws/events.log

# ═══ Ошибки за последний час ═══
grep -E '⚠️|🚨|error|exception|failed|таймаут|404|429' \
  ~/.local/share/bybit-ws/events.log | tail -30

# ═══ Watchdog kills ═══
grep "Watchdog.*завис" ~/.local/share/bybit-ws/events.log | tail -5

# ═══ Медленные циклы ═══
grep "Цикл.*превышен" ~/.local/share/bybit-ws/events.log | tail -10

# ═══ Риск-лимиты ═══
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/risk | python3 -m json.tool

# ═══ Позиции ═══
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/positions | python3 -m json.tool

# ═══ Память процесса ═══
ps -o pid,rss,vsz,comm -p $(systemctl --user show -p MainPID bybit-ws 2>/dev/null | grep -oP '\d+')

# ═══ Размер файлов данных ═══
du -sh ~/.local/share/bybit-ws/*
```

---

## Связанные документы

- [ERRORS.md](../ERRORS.md) — коды ошибок Bybit API и RPC
- [MONITOR.md](../MONITOR.md) — архитектура и мониторинг
- [AGENTS.md](../AGENTS.md) — навигация для AI-агентов
- [ARCHITECTURE.md](ARCHITECTURE.md) — детальная архитектура
- [API.md](API.md) — API-документация
