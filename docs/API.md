# bybit-ws API Reference

> Полная спецификация RPC и MCP API трейдинг-монитора bybit-ws.
> **Версия API:** v1 | **Дата:** 2026-06-18 | **Порт:** 8766

---

## Аутентификация

Все защищённые эндпоинты требуют **Bearer-токен** в заголовке `Authorization`.

### Получение токена

```bash
# Токен хранится в SQLite (автогенерация при первом запуске)
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
token = conn.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()
print(token[0]) if token else print('No token')
"
```

### Использование

```bash
TOKEN=$(python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.local/share/bybit-ws/state.db').execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])")
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/positions
```

### Сброс токена

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8766/reset-token
```

**Ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "message": "Token reset successful. Update your Authorization header.",
  "new_token": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

### Rate Limit

- **60 запросов в минуту на IP** (настраивается в `config.yaml → rpc.rate_limit_per_min`)
- При превышении: HTTP 429, `error_code: "rate_limit"`
- Восстановление: 1 токен в секунду (token bucket)

---

## Форматы ответов

### Успешный ответ

Все успешные ответы содержат `api_version: "v1"`:

```json
{
  "api_version": "v1",
  "status": "alive",
  "uptime": 84732,
  "cycle_count": 42366
}
```

### Ошибка

```json
{
  "api_version": "v1",
  "error": "Unauthorized",
  "error_code": "unauthorized",
  "detail": "Invalid or missing Bearer token",
  "status": 401
}
```

### Коды ошибок

| HTTP | `error_code` | Описание |
|------|-------------|----------|
| 400 | `bad_request` | Неверный запрос |
| 400 | `invalid_symbol` | Символ не заканчивается на USDT |
| 400 | `invalid_side` | side не Buy/Sell |
| 400 | `order_failed` | Биржа отклонила ордер |
| 401 | `unauthorized` | Неверный/отсутствует Bearer токен |
| 402 | `insufficient_margin` | Недостаточно маржи/баланса |
| 404 | `symbol_not_found` | Тикер не найден на бирже |
| 404 | `not found` | Эндпоинт не существует |
| 409 | `position_exists` | Позиция по символу уже открыта |
| 422 | `invalid_qty` | Неверное количество |
| 429 | `rate_limit` | Превышен лимит запросов |
| 500 | `internal_error` | Внутренняя ошибка сервера |

---

## RPC GET Эндпоинты

### `GET /health` — Публичный

Статус монитора. **Не требует авторизации**.

```bash
curl http://127.0.0.1:8766/health
```

**Ответ:**
```json
{
  "api_version": "v1",
  "status": "alive",
  "alive": true,
  "uptime": 84732,
  "cycle_count": 42366,
  "last_cycle": 1751114037.834,
  "cycle_duration": 2.143,
  "paused": false
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `status` | string | `"alive"` — монитор жив (<180s с последнего health) или `"stale"` |
| `alive` | bool | Активен ли монитор |
| `uptime` | int | Секунд с запуска RPC-сервера |
| `cycle_count` | int | Количество завершённых циклов |
| `last_cycle` | float | Timestamp последнего цикла |
| `cycle_duration` | float | Длительность последнего цикла в секундах |
| `paused` | bool | Приостановлена ли торговля |

---

### `GET /rpc/paths` — Публичный

Все пути установки bybit-ws. **Не требует авторизации**. Используется AI-агентами для автообнаружения.

```bash
curl http://127.0.0.1:8766/rpc/paths
```

**Ответ:**
```json
{
  "state_db": "/home/openclaw/.local/share/bybit-ws/state.db",
  "events_log": "/home/openclaw/.local/share/bybit-ws/events.log",
  "alerts_log": "/home/openclaw/.local/share/bybit-ws/alerts.log",
  "rpc_port": 8766,
  "rpc_host": "127.0.0.1",
  "repo": "/home/openclaw/bybit-ws",
  "install_dir": "/home/openclaw/.local/lib/bybit_ws",
  "config_file": "/home/openclaw/.config/bybit-ws/config.yaml",
  "service": "bybit-ws",
  "sync_command": "cp ~/bybit-ws/{file}.py ~/.local/lib/bybit_ws/",
  "restart_command": "systemctl --user restart bybit-ws",
  "venv": "/home/openclaw/bybit-ws/.venv"
}
```

---

### `GET /rpc/positions` — Защищённый

Текущие открытые позиции из SQLite SSOT.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/positions
```

**Ответ:**
```json
[
  {
    "symbol": "LINKUSDT",
    "side": "Buy",
    "entry": 8.289,
    "mark": 8.015,
    "upnl": -2.47,
    "size": 14.0,
    "stopLoss": 5.367,
    "positionIdx": 1,
    "liqPrice": 0.53,
    "leverage": 10.0,
    "positionIM": 11.61,
    "cumRealisedPnl": 0.0,
    "openTime": 1781711576,
    "margin": 11.61
  }
]
```

| Поле | Тип | Описание |
|------|-----|----------|
| `symbol` | string | Торговая пара |
| `side` | string | `"Buy"` (LONG) или `"Sell"` (SHORT) |
| `entry` | float | Цена входа |
| `mark` | float | Текущая маркировочная цена |
| `upnl` | float | Нереализованный PnL в USDT |
| `size` | float | Размер позиции в базовых единицах |
| `stopLoss` | float\|null | Цена стоп-лосса (null — не установлен) |
| `positionIdx` | int | Индекс позиции (0 — one-way, 1/2 — hedge) |
| `liqPrice` | float\|null | Цена ликвидации |
| `leverage` | float | Плечо |
| `positionIM` | float | Initial Margin (изолированная маржа) |
| `cumRealisedPnl` | float | Накопленный реализованный PnL |
| `openTime` | int | Timestamp открытия (мс) |
| `margin` | float | Маржа позиции |

**Алиас:** `GET /positions`

---

### `GET /rpc/orders` — Защищённый

Активные ордера (лимитные, SL, TP).

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/orders
```

**Ответ:**
```json
[
  {
    "symbol": "LINKUSDT",
    "orderId": "a1b2c3d4-e5f6-...",
    "status": "New",
    "kind": "TP",
    "price": 12.50,
    "trigger": 0.0,
    "qty": 14.0,
    "side": "Sell",
    "createdTime": "1751114000000",
    "cumExecQty": 0.0
  }
]
```

| Поле | Тип | Описание |
|------|-----|----------|
| `symbol` | string | Торговая пара |
| `orderId` | string | UUID ордера |
| `status` | string | Статус: `"New"`, `"PartiallyFilled"`, `"Filled"`, `"Cancelled"` |
| `kind` | string | Тип: `"SL"` (Market reduce-only), `"TP"` (Limit reduce-only), `"LIMIT_ENTRY"`, `"OTHER"` |
| `price` | float | Лимитная цена (0 для Market) |
| `trigger` | float | Триггерная цена (0 если не триггерный) |
| `qty` | float | Количество |
| `side` | string | `"Buy"` / `"Sell"` |
| `createdTime` | string | Timestamp создания (мс) |
| `cumExecQty` | float | Исполненное количество |

**Алиас:** `GET /orders`

---

### `GET /rpc/metrics` — Защищённый

Дневные торговые метрики (TP/SL/входы).

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/metrics
```

**Ответ:**
```json
{
  "api_version": "v1",
  "2026-06-17": {
    "tp_real": 1,
    "tp_false": 0,
    "sl_real": 5,
    "sl_false": 0,
    "entry": 0,
    "auto_entry_placed": 0,
    "auto_entry_filled": 0,
    "auto_entry_pnl": 0.0
  }
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `tp_real` | int | Сработавшие тейк-профиты |
| `tp_false` | int | Ложные TP-срабатывания |
| `sl_real` | int | Сработавшие стоп-лоссы |
| `sl_false` | int | Ложные SL-срабатывания |
| `entry` | int | Ручные входы |
| `auto_entry_placed` | int | Размещённые авто-входы |
| `auto_entry_filled` | int | Исполненные авто-входы |
| `auto_entry_pnl` | float | PnL от авто-входов ($) |

Ключ верхнего уровня — дата в формате `YYYY-MM-DD`.

**Алиас:** `GET /metrics` (JSON), **НЕ путать с** `GET /metrics` (Prometheus — см. ниже)

---

### `GET /rpc/risk` — Защищённый

Лимиты риска и текущее использование.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/risk
```

**Ответ (норма):**
```json
{
  "api_version": "v1",
  "blocked": false,
  "reasons": [],
  "daily_loss": 0.0,
  "max_daily_loss": 50,
  "total_margin": 86.92,
  "max_total_margin": 500,
  "position_count": 9,
  "remaining_daily_loss": 50.0,
  "remaining_margin": 413.08
}
```

**Ответ (блокировка):**
```json
{
  "api_version": "v1",
  "blocked": true,
  "reasons": [
    "daily_loss ($52.30) >= max_daily_loss ($50)",
    "total_margin ($510.00) >= max_total_margin ($500)"
  ],
  "daily_loss": -52.30,
  "max_daily_loss": 50,
  "total_margin": 510.0,
  "max_total_margin": 500,
  "position_count": 12,
  "remaining_daily_loss": 0.0,
  "remaining_margin": 0.0
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `blocked` | bool | Заблокированы ли новые входы |
| `reasons` | []string | Причины блокировки |
| `daily_loss` | float | Дневной PnL ($) |
| `max_daily_loss` | float | Лимит дневного убытка ($, default: 50) |
| `total_margin` | float | Текущая маржа ($) |
| `max_total_margin` | float | Лимит общей маржи ($, default: 500) |
| `position_count` | int | Количество открытых позиций |
| `remaining_daily_loss` | float | Остаток до лимита дневного убытка |
| `remaining_margin` | float | Остаток до лимита маржи |

**Алиас:** `GET /risk`

---

### `GET /rpc/signals` — Защищённый

Активные LONG и SHORT сигналы (скоринг, кандидаты).

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/signals
```

**Ответ:**
```json
{
  "api_version": "v1",
  "long": [
    {
      "symbol": "ADAUSDT",
      "raw": "📌 ADAUSDT score=6.6 bb_pos=35% rsi=33 tier=A",
      "score": "6.6",
      "bb_pos": "35%",
      "rsi": "33",
      "tier": "A"
    }
  ],
  "short": [
    {
      "symbol": "WLDUSDT",
      "score": 7.2,
      "tier": "A",
      "bb_pos": 88.5,
      "rsi": 72,
      "price": 0.6585,
      "lower_bb": 0.42,
      "upper_bb": 0.72
    }
  ]
}
```

**Алиас:** `GET /signals`

---

### `GET /rpc/config` — Защищённый

Текущая конфигурация монитора (без секретов — `api.key`, `api.secret`, `rpc.auth_token` скрыты).

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/config
```

**Ответ (фрагмент):**
```json
{
  "api_version": "v1",
  "api": {
    "base_url": "https://api.bytick.com",
    "retry_count": 3,
    "retry_backoff": [1, 3, 10],
    "timeout": 30
  },
  "strategy": {
    "long": {
      "leverage": 3,
      "margin_tiers": {"7": 15, "5.5": 10, "0": 5},
      "entry_offset": 0.03,
      "sl_offset": 0.07,
      "max_positions": 15
    },
    "short": {
      "leverage": 3,
      "margin": 10,
      "max_positions": 3
    }
  },
  "risk": {
    "max_drawdown_pct": 15,
    "max_total_margin": 500,
    "max_daily_loss": 50
  },
  "rpc": {
    "port": 8766,
    "bind": "127.0.0.1",
    "auth_token": "***",
    "rate_limit_per_min": 60
  }
}
```

**Алиас:** `GET /config`

---

### `GET /rpc/all` — Защищённый

Все данные одним запросом: позиции, ордера, алерты, метрики, трейды, статус монитора.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/all
```

**Ответ:**
```json
{
  "api_version": "v1",
  "positions": [...],
  "orders": [...],
  "alerts": [
    {"type": "entry", "msg": "📌 ADAUSDT score=6.6 bb_pos=35% rsi=33 tier=A"},
    {"type": "stop", "msg": "🛑 STGUSDT SL сработал @ $0.2552"}
  ],
  "metrics": {"2026-06-17": {"tp_real": 1, "sl_real": 5, ...}},
  "trades": [
    {"symbol": "ADAUSDT", "side": "Buy", "price": 0.1686, "qty": 79, "time": "..."}
  ],
  "monitor": {
    "alive": true,
    "uptime": 84732,
    "cycle_count": 42366,
    "paused": false
  }
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `positions` | array | Позиции (см. `/rpc/positions`) |
| `orders` | array | Активные ордера (см. `/rpc/orders`) |
| `alerts` | array | Последние алерты: `type` + `msg` |
| `metrics` | object | Дневные метрики (см. `/rpc/metrics`) |
| `trades` | array | Трейд-лог из `trades.jsonl` |
| `monitor` | object | Статус монитора (из `/health`) |

---

### `GET /rpc/trades` — Защищённый

Трейд-лог (последние N записей из `trades.jsonl`).

```bash
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8766/rpc/trades?limit=20"
```

**Ответ:**
```json
[
  {
    "symbol": "ADAUSDT",
    "side": "Buy",
    "price": 0.1686,
    "qty": 79,
    "time": "2026-06-17T15:42:00Z",
    "pnl": 0.0
  }
]
```

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `limit` | int | 100 | Количество последних трейдов |

---

### `GET /rpc/alerts` — Защищённый

Последние 30 алертов.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/alerts
```

**Ответ:**
```json
[
  {"msg": "🛑 STGUSDT SL сработал @ $0.2552, убыток -$3.45"},
  {"msg": "📌 ADAUSDT score=6.6 bb_pos=35% rsi=33 tier=A"}
]
```

---

### `GET /metrics` — Публичный (Prometheus)

Метрики в формате Prometheus. **Публичный эндпоинт** (без авторизации).

```bash
curl http://127.0.0.1:8766/metrics
```

**Ответ:**
```
# HELP bybit_ws_active_positions Current open positions
# TYPE bybit_ws_active_positions gauge
bybit_ws_active_positions{side="long"} 8
bybit_ws_active_positions{side="short"} 1
# HELP bybit_ws_unrealized_pnl Unrealized PnL
# TYPE bybit_ws_unrealized_pnl gauge
bybit_ws_unrealized_pnl -23.12
# HELP bybit_ws_uptime_seconds Monitor uptime
# TYPE bybit_ws_uptime_seconds gauge
bybit_ws_uptime_seconds 84732
# HELP bybit_ws_cycle_duration_seconds Last cycle duration
# TYPE bybit_ws_cycle_duration_seconds gauge
bybit_ws_cycle_duration_seconds 2.143
# HELP bybit_ws_cycle_count Total cycles
# TYPE bybit_ws_cycle_count counter
bybit_ws_cycle_count 42366
# HELP bybit_ws_daily_pnl Daily realized PnL
# TYPE bybit_ws_daily_pnl gauge
bybit_ws_daily_pnl 0.00
```

| Метрика | Тип | Описание |
|---------|-----|----------|
| `bybit_ws_active_positions` | gauge | Активные позиции с лейблами `side="long"`/`side="short"` |
| `bybit_ws_unrealized_pnl` | gauge | Нереализованный PnL ($) |
| `bybit_ws_uptime_seconds` | gauge | Аптайм монитора (сек) |
| `bybit_ws_cycle_duration_seconds` | gauge | Длительность последнего цикла (сек) |
| `bybit_ws_cycle_count` | counter | Всего циклов |
| `bybit_ws_daily_pnl` | gauge | Дневной реализованный PnL ($) |

---

### `GET /rpc` или `GET /` — Защищённый

Список всех доступных эндпоинтов.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc
```

**Ответ:**
```json
{
  "api_version": "v1",
  "service": "bybit-ws-rpc",
  "endpoints": [
    "/rpc/all", "/rpc/positions", "/rpc/orders",
    "/rpc/health", "/rpc/trades", "/rpc/alerts", "/rpc/metrics", "/rpc/risk",
    "/rpc/signals", "/rpc/config", "/rpc/paths",
    "/health", "/positions", "/orders", "/metrics", "/risk", "/signals", "/config",
    "POST /scan", "POST /enter", "POST /close", "POST /reset-token",
    "POST /reload-config", "POST /pause", "POST /resume", "POST /logs"
  ]
}
```

---

## RPC POST Эндпоинты

### `POST /scan` — Защищённый

Запустить сканирование сигналов Bollinger Grid.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode": "long", "interval": "D", "limit": 5}' \
  http://127.0.0.1:8766/scan
```

**Тело запроса:**

| Параметр | Тип | Обязательный | По умолчанию | Описание |
|----------|-----|-------------|-------------|----------|
| `mode` | string | Нет | `"long"` | `"long"` или `"short"` |
| `interval` | string | Нет | `"D"` | Таймфрейм: `"D"`, `"W"`, `"4h"`, `"1h"`, `"15m"`, `"5m"` |
| `limit` | int | Нет | 5 | Количество результатов (1–20) |

**Успешный ответ:**
```json
{
  "api_version": "v1",
  "mode": "long",
  "count": 5,
  "signals": [
    {
      "symbol": "ADAUSDT",
      "score": 6.6,
      "tier": "A",
      "bb_pos": 35.2,
      "rsi": 33.1,
      "price": 0.1697,
      "lower_bb": 0.1315,
      "upper_bb": 0.2079,
      "middle_bb": 0.1697
    }
  ]
}
```

**Ошибки:**
- `400` — `mode` не `long`/`short`, или `limit` не число, или `limit` вне 1–20
- `500` — ошибка в сканере

---

### `POST /enter` — Защищённый

Вход в позицию (Market или Limit) с двухэтапным подтверждением.

#### Этап 1: Превью (confirm: false)

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "LINKUSDT", "side": "Buy", "qty": 14, "sl": 5.31, "tp": 12.50, "confirm": false}' \
  http://127.0.0.1:8766/enter
```

**Ответ превью:**
```json
{
  "api_version": "v1",
  "symbol": "LINKUSDT",
  "side": "Buy",
  "qty": 14,
  "sl": 5.31,
  "tp": 12.5,
  "confirm_required": true,
  "message": "Send with confirm: true to execute"
}
```

#### Этап 2: Исполнение (confirm: true)

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "LINKUSDT", "side": "Buy", "qty": 14, "sl": 5.31, "tp": 12.50, "confirm": true}' \
  http://127.0.0.1:8766/enter
```

**Тело запроса:**

| Параметр | Тип | Обязательный | Описание |
|----------|-----|-------------|----------|
| `symbol` | string | ✅ | Торговая пара (должна заканчиваться на USDT) |
| `side` | string | ✅ | `"Buy"` (LONG) или `"Sell"` (SHORT) |
| `qty` | float | ✅ | Количество в базовых единицах (>0) |
| `sl` | float | Нет | Цена стоп-лосса |
| `tp` | float | Нет | Цена тейк-профита |
| `confirm` | bool | Нет | `false` — превью, `true` — исполнение |
| `order_type` | string | Нет | `"Market"` (default) или `"Limit"` |
| `price` | float | Нет | Лимитная цена (только для Limit) |

**Успешный ответ (Market):**
```json
{
  "api_version": "v1",
  "status": "ok",
  "symbol": "LINKUSDT",
  "side": "Buy",
  "qty": 14.0,
  "order_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "sl": {
    "price": 5.31,
    "status": "placed"
  },
  "tp": {
    "price": 12.5,
    "status": "placed"
  }
}
```

**Ошибки:**
| HTTP | `error_code` | Условие |
|------|-------------|---------|
| 400 | `invalid_symbol` | Символ не заканчивается на USDT |
| 400 | `invalid_side` | side не Buy/Sell |
| 400 | `invalid_qty` | qty не число |
| 409 | `position_exists` | Позиция по символу уже открыта |
| 422 | `invalid_qty` | qty ≤ 0 |
| 402 | `insufficient_margin` | Недостаточно баланса/маржи |
| 404 | `symbol_not_found` | Символ не найден на бирже |
| 400 | `order_failed` | Биржа отклонила ордер |

---

### `POST /close` — Защищённый

Закрыть позицию по рынку.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "LINKUSDT"}' \
  http://127.0.0.1:8766/close
```

**Тело запроса:**

| Параметр | Тип | Обязательный | Описание |
|----------|-----|-------------|----------|
| `symbol` | string | ✅ | Торговая пара для закрытия |

**Успешный ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "symbol": "LINKUSDT",
  "closed_side": "Buy",
  "size": 14.0,
  "entry": 8.289,
  "mark": 8.015,
  "close_side": "Sell",
  "order_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901"
}
```

**Ошибки:**
- `400` — неверный символ
- `404` — позиция не найдена
- `400` — ошибка закрытия на бирже

---

### `POST /reload-config` — Защищённый

Перечитать `config.yaml` без перезапуска сервиса.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8766/reload-config
```

**Успешный ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "message": "config reloaded"
}
```

**Ошибки:** `500` — ошибка чтения/парсинга конфига

---

### `POST /pause` — Защищённый

Приостановить торговлю (новые входы блокируются, текущие позиции не трогаются).

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8766/pause
```

**Ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "paused": true
}
```

---

### `POST /resume` — Защищённый

Возобновить торговлю после паузы.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8766/resume
```

**Ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "paused": false
}
```

---

### `POST /logs` — Защищённый

Получить последние строки `events.log`.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"lines": 50}' \
  http://127.0.0.1:8766/logs
```

**Тело запроса:**

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `lines` | int | 100 | Количество последних строк |

**Успешный ответ:**
```json
{
  "api_version": "v1",
  "lines": 50,
  "log": "[2026-06-18 05:41:25] Монитор запущен: 7 позиций, 22 ордеров\n[2026-06-18 05:42:31] 🛑 Корреляция 86% LONG — авто-вход заблокирован\n..."
}
```

---

### `POST /reset-token` — Защищённый

Сбросить RPC-токен (генерирует новый UUID). Требует действующий токен.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8766/reset-token
```

**Ответ:**
```json
{
  "api_version": "v1",
  "status": "ok",
  "message": "Token reset successful. Update your Authorization header.",
  "new_token": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

---

## MCP Инструменты (bybit-mcp-server)

MCP-сервер (`~/.local/bin/bybit-mcp-server.py`) работает через stdio и предоставляет инструменты для AI-агентов. Проксирует запросы к RPC-серверу на порту 8766.

### `scan_market`

Сканирование рынка на сигналы Bollinger Grid.

| Параметр | Тип | Обязательный | По умолчанию | Описание |
|----------|-----|-------------|-------------|----------|
| `mode` | string | Нет | `"long"` | `"long"` или `"short"` |
| `interval` | string | Нет | `"D"` | `"D"`, `"W"`, `"4h"`, `"1h"`, `"15m"`, `"5m"` |
| `limit` | int | Нет | 10 | Макс. результатов |

**Пример вызова (через MCP):**
```python
scan_market(mode="long", interval="D", limit=5)
```

**Пример ответа:**
```
Top LONG candidates:
  ADAUSDT     Score=6.6  Tier=A  BB=35%  RSI=33  $0.1697 → entry $0.1315
  DOTUSDT     Score=5.8  Tier=A  BB=28%  RSI=38  $1.0108 → entry $0.8001
```

---

### `get_positions`

Текущие позиции с нереализованным PnL, стоп-лоссами и плечом.

**Параметры:** нет

**Пример вызова (через MCP):**
```python
get_positions()
```

**Пример ответа:**
```
🟢 WLDUSDT      Sell 10.0x  entry=$0.6585  mark=$0.6444  PnL=+$2.29  SL=$0.725
🔴 LINKUSDT     Buy  10.0x  entry=$8.2890  mark=$8.0150  PnL=-$2.47  SL=$5.367
🔴 ADAUSDT      Buy  10.0x  entry=$0.1686  mark=$0.1665  PnL=-$1.65  SL=$0.1226

Total unrealized PnL: -$23.12
```

Иконки: 🟢 = прибыль, 🔴 = убыток.

---

### `get_metrics`

Дневные метрики: TP/SL счёт, входы, авто-входы.

**Параметры:** нет

**Пример вызова (через MCP):**
```python
get_metrics()
```

**Пример ответа:**
```
Today's metrics:
  TP: 1  SL: 5
  Entries: 0  Auto-filled: 0
  Auto-entry PnL: $0.00
```

---

### `get_risk_status`

Лимиты риска и текущее использование: дневной PnL, маржа, блокировки.

**Параметры:** нет

**Пример вызова (через MCP):**
```python
get_risk_status()
```

**Пример ответа (🟢 OK):**
```
Risk Status: 🟢 OK
  Daily PnL: $0.00 / -$50
  Margin: $86.92 / $500
  Positions: 9
  Remaining: $50.00 loss / $413.08 margin
  Block reasons:
  (none)
```

**Пример ответа (🛑 BLOCKED):**
```
Risk Status: 🛑 BLOCKED
  Daily PnL: -$52.30 / -$50
  Margin: $140.00 / $500
  Positions: 5
  Remaining: $0.00 loss / $360.00 margin
  Block reasons:
  • daily_loss ($52.30) >= max_daily_loss ($50)
```

---

### `place_entry`

Вход в позицию (Market или Limit) с опциональными SL/TP.

| Параметр | Тип | Обязательный | Описание |
|----------|-----|-------------|----------|
| `symbol` | string | ✅ | Торговая пара, например `"LINKUSDT"` |
| `side` | string | ✅ | `"Buy"` (LONG) или `"Sell"` (SHORT) |
| `qty` | float | ✅ | Количество в базовых единицах (например, 14 LINK) |
| `sl` | float | Нет | Цена стоп-лосса |
| `tp` | float | Нет | Цена тейк-профита |
| `order_type` | string | Нет | `"Market"` (default) или `"Limit"` |
| `price` | float | Нет | Лимитная цена (требуется для Limit) |

**Пример вызова (через MCP):**
```python
# Market LONG
place_entry(symbol="LINKUSDT", side="Buy", qty=14, sl=5.31, tp=12.50)

# Limit SHORT на BB-полосе
place_entry(symbol="WLDUSDT", side="Sell", qty=25, sl=0.725, tp=0.42, order_type="Limit", price=0.72)
```

**Пример ответа (успех):**
```
✅ Entry: LINKUSDT Buy 14.0шт (Market)
   Order ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890
   SL: $5.31 (placed)
   TP: $12.50 (placed)
```

**Пример ответа (ошибка):**
```
❌ Entry failed: Position already exists: LINKUSDT already has an open Buy position of size 14.0
```

**Важно:** если `qty=0`, MCP-сервер пытается рассчитать размер через ATR-сайзинг автоматически.

---

### `vpn_status`

Статус VPN: сервис, трафик, подключённые клиенты.

**Параметры:** нет

**Пример вызова (через MCP):**
```python
vpn_status()
```

**Пример ответа:**
```
VPN Status: ✅
  Service: active  Port: open
  Traffic: ↓18.9 KB/s  ↑22.0 KB/s
  Errors: 0
  Clients: 5
```

---

## Типичный воркфлоу AI-агента

```
1. scan_market(mode="long", interval="D")
   → выбрать топ-кандидатов по скору

2. get_risk_status()
   → проверить: blocked=false, остаток маржи достаточен

3. get_positions()
   → убедиться, что позиции по выбранному символу ещё нет

4. place_entry(symbol="ADAUSDT", side="Buy", qty=79, sl=0.1226, tp=0.25)
   → войти с SL/TP
```

---

## Полная таблица эндпоинтов

| Метод | Путь | Auth | Описание |
|-------|------|------|----------|
| GET | `/health` | Нет | Статус монитора (alive, uptime, cycle_count) |
| GET | `/rpc/paths` | Нет | Пути установки |
| GET | `/metrics` | Нет | Prometheus-метрики |
| GET | `/`, `/rpc` | Да | Список эндпоинтов |
| GET | `/rpc/all` | Да | Все данные разом |
| GET | `/rpc/positions`, `/positions` | Да | Текущие позиции |
| GET | `/rpc/orders`, `/orders` | Да | Активные ордера |
| GET | `/rpc/metrics` | Да | Дневные метрики (JSON) |
| GET | `/rpc/risk`, `/risk` | Да | Лимиты риска |
| GET | `/rpc/signals`, `/signals` | Да | LONG/SHORT сигналы |
| GET | `/rpc/config`, `/config` | Да | Конфигурация (без секретов) |
| GET | `/rpc/trades` | Да | Трейд-лог |
| GET | `/rpc/alerts` | Да | Последние алерты |
| POST | `/scan` | Да | Сканирование сигналов |
| POST | `/enter` | Да | Вход в позицию |
| POST | `/close` | Да | Закрытие позиции |
| POST | `/reload-config` | Да | Перечитать конфиг |
| POST | `/pause` | Да | Приостановить торговлю |
| POST | `/resume` | Да | Возобновить торговлю |
| POST | `/logs` | Да | Последние строки лога |
| POST | `/reset-token` | Да | Сбросить RPC-токен |

---

## CORS

RPC-сервер поддерживает CORS для локальных источников:

- `Access-Control-Allow-Origin`: `http://localhost, http://127.0.0.1`
- `Access-Control-Allow-Methods`: `GET, POST, OPTIONS`
- `Access-Control-Allow-Headers`: `Content-Type, Authorization`
- Preflight (`OPTIONS`) возвращает 200 без авторизации
