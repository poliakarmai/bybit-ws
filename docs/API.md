# bybit-ws API Reference

> REST API трейдинг-монитора bybit-ws. v7.2 (28.06.2026).

## Аутентификация

`Authorization: Bearer *** All endpoints.

⚠️ **Production: HTTPS only** (nginx + Let's Encrypt или Cloudflare Tunnel).

**Ротация токена:** `POST /reset-token`. Grace period: старый токен работает ещё 5 минут.

## Формат ответов

### Успех
```json
{"api_version":"v1","status":"ok","<data>":"..."}
```

### Ошибка
```json
{"api_version":"v1","error":"описание","detail":"подробности","code":400,"error_code":"invalid_symbol"}
```

Коды: `200` (успех), `400` (параметры), `401` (токен), `404` (нет ресурса), `429` (rate limit), `500` (сервер), `502` (Bybit API).

## Типы данных

⚠️ Все финансовые значения — **строка** (string) для исключения floating-point ошибок.

## Rate Limits

| Группа | Лимит |
|--------|-------|
| `/enter`, `/close`, `/move_sl` | 10 запросов/мин |
| `/scan` | 30 запросов/мин |
| `/metrics` | 60 запросов/мин |
| Остальные GET | 100 запросов/мин |

---

## Эндпоинты

### Дашборд (GET)

| Путь | Описание |
|------|---------|
| `/positions` | Все открытые позиции + нереализованный PnL |
| `/risk` | Дневной PnL, маржа, CB, корреляции |
| `/circuit_breaker` | Статус CB |
| `/balance` | Баланс кошелька (available, margin, equity) |
| `/orders` | Активные ордера (SL/TP) |
| `/alerts` | Накопленные алерты (с пагинацией: `?limit=100&offset=0`) |
| `/trades` | История сделок (с пагинацией: `?limit=50&offset=0`) |
| `/signals` | Последние BB-сигналы |
| `/config` | Конфигурация (без кредов) |
| `/health` | Статус: alive + timestamp цикла |
| `/summary` | Краткая сводка (позиции + PnL + алерты) |

### `/positions`

```json
// GET /positions
{
  "api_version":"v1",
  "positions":{
    "AAVEUSDT":{"symbol":"AAVEUSDT","side":"Sell","leverage":"10","entryPrice":"98.60","markPrice":"93.50","unrealisedPnl":"4.18","stopLoss":"95.00","takeProfit":"90.00","size":"1"}
  },
  "total_unrealised_pnl":"4.18",
  "count":1
}
```

### Управление (POST)

| Путь | Описание | Идемпотентность |
|------|---------|----------------|
| `/enter` | Вход в позицию | ✅ `Idempotency-Key` (UUID) |
| `/close` | Закрыть позицию | — |
| `/move_sl` | Подвинуть SL/TP | — |
| `/cancel_order` | Отменить ордер по order_id | ✅ |
| `/order` | Статус ордера по order_id | GET |

#### `/enter`

```json
// POST /enter
// Headers: Idempotency-Key: <UUID>
{
  "symbol": "LINKUSDT",  // required
  "side": "Sell",        // "Buy" (LONG) | "Sell" (SHORT)
  "qty": 14,             // required
  "sl": 15.50,           // optional
  "tp": 14.00,           // optional
  "confirm": true        // true = исполнить, false = preview
}

// Ответ (confirm=true)
{"api_version":"v1","status":"ok","order_id":"a1b2...","symbol":"LINKUSDT","side":"Sell","qty":"14","entry_price":"15.23","sl":"15.50","tp":"14.00"}

// Ответ (confirm=false — preview)
{"api_version":"v1","preview":{"symbol":"LINKUSDT","side":"Sell","qty":"14","estimated_entry":"15.23","estimated_sl_distance":"1.77%","estimated_tp_distance":"8.07%","risk_reward_ratio":"4.56"}}
```

#### `/close`

```json
// POST /close
{"symbol": "LINKUSDT"}
// Ответ: {"api_version":"v1","status":"ok","symbol":"LINKUSDT","closed":true}
```

#### `/move_sl`

```json
// POST /move_sl
{"symbol":"AAVEUSDT","stop_loss":"95.00","take_profit":"90.00"}
// Ответ: {"api_version":"v1","status":"ok","symbol":"AAVEUSDT","old_sl":"96.60","new_sl":"95.00","new_tp":"90.00"}
```

#### `/cancel_order`

```json
// POST /cancel_order
{"order_id":"a1b2c3d4-..."}
// Ответ: {"api_version":"v1","status":"ok","order_id":"a1b2c3d4-...","cancelled":true}
```

#### `/order`

```json
// GET /order?id=a1b2c3d4-...
// Ответ: {"api_version":"v1","order":{"order_id":"a1b2...","symbol":"LINKUSDT","type":"LIMIT","price":"15.50","qty":"14","status":"New"}}
```

### Скан

```
POST /scan
```

```json
// Запрос
{"mode":"short","interval":"D","limit":10}
// mode: "long" | "short"
// interval: "D" | "W" | "4h" | "1h" | "15m" | "5m"
// limit: 1-20 (default 5)
```

### Аварийные

| Путь | Описание | Auth |
|------|---------|------|
| `/emergency_close` | Закрыть ВСЕ позиции по рынку | `X-Emergency-Auth` |
| `/emergency_close` + `{symbol}` | Закрыть одну позицию | `X-Emergency-Auth` |
| `/kill_switch` | Закрыть всё + блокировка новых входов | `X-Emergency-Auth` |
| `/circuit_breaker` POST `{"action":"reset"}` | Сброс CB | Bearer |

**Kill Switch:** полная блокировка до `/resume`. Без таймаута.

### Служебные

| Путь | Описание |
|------|---------|
| `POST /reset-token` | Новый токен (старый + grace 5 мин) |
| `POST /pause` | Пауза авто-трейдинга |
| `POST /resume` | Возобновить |
| `POST /reload-config` | Перезагрузить конфиг |
| `POST /set_leverage` | `{"symbol":"LINKUSDT","leverage":10}` |

## WebSocket API

`wss://<host>:8766/ws`

Push-уведомления для real-time клиентов (Android, Web):

| Событие | Данные |
|---------|--------|
| `position_update` | Позиция изменилась (PnL, марка, SL/TP) |
| `alert` | Новый алерт (ENTRY, TP, SL, PUMP, CB) |
| `order_update` | Ордер исполнен/отменён |
| `heartbeat` | Каждые 30с |

Аутентификация: `?token=<bearer_token>` в URL при подключении.

## Webhook (TradingView)

```
POST /webhook
```

```json
{
  "symbol": "LINKUSDT",
  "side": "Sell",
  "qty": 14,
  "secret": "<webhook_secret>"
}
```

## Prometheus

`GET /metrics` (Bearer auth). Рекомендуется вынести на отдельный порт (:9090).

## CORS

```
Access-Control-Allow-Origin: https://ваш-домен
Access-Control-Allow-Headers: Authorization, Content-Type, Idempotency-Key
```

## MCP-инструменты (через Hermes)

| Инструмент | → REST |
|-----------|--------|
| `bybit_ws.scan_market(mode, interval, limit)` | `POST /scan` |
| `bybit_ws.get_positions()` | `GET /positions` |
| `bybit_ws.get_metrics()` | `GET /metrics` (Prometheus) |
| `bybit_ws.get_risk_status()` | `GET /risk` |
| `bybit_ws.place_entry(symbol, side, qty, sl, tp)` | `POST /enter` |
| `bybit_ws.get_journal()` | `GET /trades` (journal не отдельный эндпоинт) |

## OpenAPI

`GET /openapi.json` — OpenAPI 3.0 спецификация для автогенерации клиентов.

## Безопасность (чек-лист)

- [x] HTTPS (nginx + Let's Encrypt)
- [x] Bearer-токен + ротация
- [x] X-Emergency-Auth для критических операций
- [x] Rate limiting (nginx + встроенный)
- [x] Idempotency-Key для /enter
- [x] Grace period для /reset-token (5 минут)
- [x] CORS для Web-клиентов
- [ ] `/logs` — отключен в проде (утечка токенов)
- [ ] `/paths` — только с X-Admin-Auth

## Changelog

| Дата | Изменение |
|------|----------|
| 28.06.2026 v7.1 | REST (не JSON-RPC), унификация путей, типы string, rate limits, Idempotency-Key, WebSocket API, OpenAPI, CORS, /cancel_order, /order, /balance, /summary, webhook |
| 28.06.2026 v7.2 | Paper Trading: /paper/balance, /paper/positions, /paper/summary |

## Paper Trading API (v7.2)

`BYBIT_PAPER_ENABLED=1` для активации.

### GET /paper/balance
Возвращает баланс paper-счёта.
```json
{"enabled": true, "balance": 10000.0, "currency": "USDT"}
```

### GET /paper/positions
Возвращает открытые paper-позиции.
```json
{"enabled": true, "positions": [{"symbol": "BTCUSDT", "side": "Buy", "size": 0.01, "entry": 85000, "mark": 85500, "upnl": 5.0, "leverage": 3}], "count": 1}
```

### GET /paper/summary
Сводка paper-торговли: баланс, кол-во позиций, total_pnl, total_fees, upnl.
```json
{"enabled": true, "balance": 10150.0, "positions": 2, "total_pnl": 120.0, "total_fees": 30.0, "trades": 15, "upnl": 15.0}
```
