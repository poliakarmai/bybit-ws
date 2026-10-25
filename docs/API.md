# bybit-ws API Reference

> HTTP REST API трейдинг-монитора (сервер `rpc.py`, **`http.server`**, не JSON-RPC).
> Проверено по коду 2026-10-25, версия продукта 11.5.0, `api_version: "v1"` в каждом ответе.
> Источник истины — таблица маршрутов в `rpc.py` (`do_GET` / `do_POST`); всё, чего там нет, не существует.

## Аутентификация

`Authorization: Bearer <token>`; токен — `kv_store['rpc_auth_token']` в `state.db`, либо JWT (Android).
Ротация: `POST /reset-token`.

**Без токена (публичные):** `/health`, `/rpc/paths`, `/dashboard`, `/rpc/dash/all`, `/rpc/dash/risk`, `/rpc/dash/signals`.

**Аварийные** (`/kill_switch`, `/emergency_close`) требуют **два** заголовка: `Authorization: Bearer …` **и** `X-Emergency-Auth: $EMERGENCY_TOKEN` (из env-файла сервиса).

⚠️ Production: только через nginx/Cloudflare (TLS снаружи), сам сервер слушает `127.0.0.1:8766`.

## Формат ответов

```json
{"api_version":"v1","status":"ok","<data>":"..."}
{"api_version":"v1","error":"описание","detail":"подробности","status":400}
```

Коды: `200`, `400` (невалидный JSON/параметры), `401` (токен), `404` (нет маршрута), `429` (rate limit), `500`.

## Rate limit

Один лимит на IP: `rpc.rate_limit_per_min` из конфига (дефолт **60/мин**), применяется к GET и POST одинаково. Разных лимитов по эндпоинтам нет.

---

## GET

### Публичные

| Путь | Назначение |
|------|-----------|
| `/health` | `status` (`alive`/`stale`), `alive`, `uptime`, `cycle_count`, `trading_ready` (пауза/CB → false) |
| `/rpc/paths` | Пути установки (state.db, events.log, конфиг, порт) для внешних агентов |
| `/dashboard` | HTML-дашборд |
| `/rpc/dash/all` · `/rpc/dash/risk` · `/rpc/dash/signals` | Данные для дашборда (all / risk / signals) |

### С токеном

| Путь | Назначение |
|------|-----------|
| `/positions` · `/rpc/positions` | Открытые позиции + uPnL (`side` сырой Bybit: **Buy=LONG, Sell=SHORT**) |
| `/orders` · `/rpc/orders` | Активные ордера (SL/TP) |
| `/balance` | Баланс: `walletBalance`, `available`, `equity` |
| `/risk` · `/rpc/risk` · `/rpc/risk_full` | Лимиты, дневной PnL, circuit breaker, корреляции |
| `/rpc/circuit_breaker` | Статус CB (сброс — рестарт сервиса, отдельного POST нет) |
| `/signals` · `/rpc/signals` | Последние BB-сигналы LONG/SHORT |
| `/config` · `/rpc/config` | Конфигурация без секретов |
| `/rpc/trades` | История сделок из `trades.jsonl` |
| `/rpc/alerts` | Последние алерты |
| `/rpc/all` | Всё одним запросом (позиции, ордера, алерты, метрики, трейды) |
| `/rpc/metrics` | Метрики (JSON) |
| `/metrics` | **Prometheus** text/plain (`bybit_ws_active_positions`, `bybit_ws_unrealized_pnl`, `bybit_ws_uptime_seconds`, `bybit_ws_cycle_duration_seconds`, `bybit_ws_cycle_count`, `bybit_ws_daily_pnl`) |
| `/rpc/ab_test_report` · `/rpc/ab_status` | A/B-отчёт и статус |
| `/rpc/ml_toggle` | Тумблер ML-гейта |
| `/rpc/symbol_stats` | Статистика по символам |
| `/rpc/analyze_history` | История анализа |

`/positions`:

```json
{"api_version":"v1","positions":{"AAVEUSDT":{"symbol":"AAVEUSDT","side":"Sell","leverage":"10",
  "entryPrice":"98.60","markPrice":"93.50","unrealisedPnl":"4.18","stopLoss":"95.00","size":"1"}},
 "total_unrealised_pnl":"4.18","count":1}
```

## POST (Bearer обязателен)

| Путь | Назначение | Тело |
|------|-----------|------|
| `/scan` | GridSignal-скан | `{mode:"long"\|"short", interval:"D"\|"W"\|"4h"\|"1h"\|"15m"\|"5m", symbol?}` |
| `/calc_qty` | Расчёт размера | `{symbol, risk_pct, leverage}` |
| `/enter` | Вход | `{symbol, side, qty, sl?, tp?, leverage?, confirm}` |
| `/close` | Закрыть позицию | `{symbol}` |
| `/move_sl` | Двинуть SL/TP | `{symbol, stop_loss, take_profit?}` |
| `/set-tp` | Поставить TP | `{symbol, tp}` |
| `/kill_switch` | Закрыть всё + блокировка до `/resume` | — · **+ `X-Emergency-Auth`** |
| `/emergency_close` | Закрыть всё без блокировки | `{symbol?}` · **+ `X-Emergency-Auth`** |
| `/pause` · `/resume` | Пауза/возобновление авто-входов (все источники) | — |
| `/reload-config` | Перечитать конфиг | — |
| `/logs` | Последние строки лога | `{lines?}` |
| `/reset-token` | Новый RPC-токен | — |
| `/generate-jwt` | JWT для Android | — |
| `/paper/balance` · `/paper/positions` · `/paper/summary` | Paper Trading (`BYBIT_PAPER_ENABLED=1`) | — |

### `/enter` — двухшаговый confirm

```bash
# 1) превью
curl -s -X POST http://127.0.0.1:8766/enter -H "Authorization: Bearer $TOKEN" \
  -d '{"symbol":"LINKUSDT","side":"Sell","qty":14,"sl":15.50,"tp":14.00}'
# 2) исполнение (без confirm:true ордер НЕ ставится)
curl -s -m 60 -X POST http://127.0.0.1:8766/enter -H "Authorization: Bearer $TOKEN" \
  -d '{"symbol":"LINKUSDT","side":"Sell","qty":14,"sl":15.50,"tp":14.00,"confirm":true}'
```

`side`: `Buy`=LONG, `Sell`=SHORT. `leverage` (1–100) необязателен: передан → ставится ровно он, не передан → плечо не навязывается (`/enter`), авто-ветка выставляет конфигное через `api.set_leverage()`. `-m 60` обязателен: Bybit отвечает до 30–40 с.

## WebSocket

`ws://127.0.0.1:8768/ws` — **отдельный порт** (aiohttp, поднимается из `main_async.py`; дефолт в `start_ws_server()` — 8767, живой запуск — 8768). Не 8766.

Push: снапшот состояния при подключении (`position_update`, `alert`, `order_update`).
Аутентификация — сетевой уровень (bind на loopback), токена в URL нет.

## Чего в API НЕТ

Не изобретать: `/alerts`, `/trades`, `/summary`, `/cancel_order`, `/order`, `/webhook`, `/openapi.json`, `/set_leverage`, `POST /circuit_breaker`. Сброс CB — рестарт сервиса. `/rpc/all` существует, но как JSON-агрегат.

## MCP-инструменты (через Hermes)

| Инструмент | → REST |
|-----------|--------|
| `scan_market(mode, interval)` | `POST /scan` |
| `get_positions()` | `GET /positions` |
| `get_metrics()` | `GET /rpc/metrics` |
| `get_risk_status()` | `GET /risk` |
| `place_entry(symbol, side, qty, sl, tp, order_type, price, leverage)` | `POST /enter` |
| `get_journal()` | `GET /rpc/trades` + FIFO-анализ |

## Изменения

| Дата | Что |
|------|-----|
| 2026-10-25 | Файл переписан по фактическим маршрутам `rpc.py`: удалены несуществующие эндпоинты, исправлены порт WS (8768), rate limit (один на IP), публичный список, `/health.trading_ready`, `X-Emergency-Auth` |
| 2026-06-28 v7.1 | REST вместо JSON-RPC, типы string, Idempotency-Key, `/balance`, `/summary` |
| 2026-06-29 Фаза 7 | Paper Trading `/paper/*` |
